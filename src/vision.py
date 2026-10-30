"""Vision provider used by `describe`.

Callers only use describe_product(). The body talks to Anthropic today;
swap the body (not the signature) to move to another provider.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

import anthropic
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

import config
from src.utils import LOG

TOOL_NAME = "record_product"

# Transport and transient server failures. The SDK's own retries are disabled
# in _client() so this list is the only retry policy (5 attempts).
RETRYABLE = (
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.OverloadedError,
    anthropic.ServiceUnavailableError,
    anthropic.DeadlineExceededError,
)


class InvalidListingError(ValueError):
    """The model returned a listing that failed enum checks after a retry."""


def _client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    return anthropic.Anthropic(api_key=api_key, timeout=60.0, max_retries=0)


def _tool_schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": "Record the drafted marketplace listing for this product.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short specific title, under 80 characters. No emoji.",
                },
                "description": {
                    "type": "string",
                    "description": "One or two factual sentences based only on the photos.",
                },
                "category": {
                    "type": "string",
                    "enum": list(config.ALLOWED_CATEGORIES),
                },
                "condition": {
                    "type": "string",
                    "enum": list(config.ALLOWED_CONDITIONS),
                },
            },
            "required": ["title", "description", "category", "condition"],
        },
    }


def _system_prompt() -> str:
    categories = ", ".join(config.ALLOWED_CATEGORIES)
    conditions = ", ".join(config.ALLOWED_CONDITIONS)
    return (
        "You draft a marketplace listing from photos of a single product. "
        "Call the record_product tool exactly once.\n"
        "title: short and specific, under 80 characters, no emoji, no surrounding quotes.\n"
        "description: one or two sentences. Describe only what is visible. "
        "Do not invent a brand, price, size, or defect you cannot see.\n"
        f"category: exactly one of: {categories}.\n"
        f"condition: exactly one of: {conditions}.\n"
        "If the category is unclear, use Misc. If the condition is unclear, use Used."
    )


def _message_content(images: list[bytes], reminder: str | None) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for data in images:
        encoded = base64.standard_b64encode(data).decode("ascii")
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": encoded,
                },
            }
        )
    text = "These photos are different views of one product. Draft the listing."
    if reminder:
        text = f"{text}\n{reminder}"
    content.append({"type": "text", "text": text})
    return content


def parse_tool_payload(content: list[Any]) -> dict[str, Any]:
    for block in content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == TOOL_NAME:
            payload = block.input
            if isinstance(payload, dict):
                return payload
    raise InvalidListingError("Model response did not include a record_product tool call")


def validate_listing(payload: dict[str, Any]) -> dict[str, str]:
    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip()
    category = str(payload.get("category") or "").strip()
    condition = str(payload.get("condition") or "").strip()
    if not title or not description:
        raise InvalidListingError("Title and description must be non-empty")
    if category not in config.ALLOWED_CATEGORIES:
        raise InvalidListingError(f"Category {category!r} is not allowed")
    if condition not in config.ALLOWED_CONDITIONS:
        raise InvalidListingError(f"Condition {condition!r} is not allowed")
    return {
        "title": title[:120],
        "description": description,
        "category": category,
        "condition": condition,
    }


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    retry=retry_if_exception_type(RETRYABLE),
    before_sleep=before_sleep_log(LOG, logging.WARNING),
)
def _create_message(client: anthropic.Anthropic, *, model: str, images: list[bytes], reminder: str | None):
    LOG.debug("Calling vision model %s with %d image(s)", model, len(images))
    return client.messages.create(
        model=model,
        max_tokens=1024,
        system=_system_prompt(),
        tools=[_tool_schema()],
        tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=[{"role": "user", "content": _message_content(images, reminder)}],
    )


def describe_product(images: list[bytes], *, model: str) -> dict[str, str]:
    """Return title, description, category, and condition for one product.

    Raises RuntimeError if the API key is missing, anthropic.APIError if the
    request still fails after retries, or InvalidListingError if the listing
    is unusable after one corrective attempt.
    """
    if not images:
        raise ValueError("describe_product requires at least one image")

    client = _client()
    reminder = None
    last_error: InvalidListingError | None = None
    for attempt in (1, 2):
        response = _create_message(client, model=model, images=images, reminder=reminder)
        try:
            payload = parse_tool_payload(response.content)
            return validate_listing(payload)
        except InvalidListingError as exc:
            last_error = exc
            LOG.warning("Listing failed validation (attempt %d): %s", attempt, exc)
            reminder = (
                "Your previous answer was rejected. "
                f"category MUST be exactly one of: {', '.join(config.ALLOWED_CATEGORIES)}. "
                f"condition MUST be exactly one of: {', '.join(config.ALLOWED_CONDITIONS)}. "
                "Call record_product again with those exact strings."
            )
    assert last_error is not None
    raise last_error
