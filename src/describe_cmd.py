"""Draft a title, description, category, and condition for each product folder.

Reads whatever images are currently in output/product-*/, so Finder fixes
made after `group` are what the model sees. Results are cached by a
fingerprint of the folder's filenames and sizes.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import anthropic

import config
from src.imaging import ImageReadError, image_pixel_count, jpeg_bytes_max_edge
from src.utils import LOG, atomic_write_json, load_json
from src.vision import InvalidListingError, describe_product

PRODUCT_PREFIX = "product-"


def product_dirs() -> list[Path]:
    if not config.OUTPUT_DIR.is_dir():
        return []
    dirs = [
        path
        for path in config.OUTPUT_DIR.iterdir()
        if path.is_dir() and path.name.startswith(PRODUCT_PREFIX)
    ]
    return sorted(dirs, key=lambda path: path.name)


def images_in(folder: Path) -> list[Path]:
    files = [
        path
        for path in folder.iterdir()
        if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in config.IMAGE_EXTENSIONS
    ]
    return sorted(files, key=lambda path: path.name.lower())


def folder_fingerprint(files: list[Path]) -> str:
    parts = [f"{path.name}:{path.stat().st_size}" for path in files]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def representative_images(files: list[Path], limit: int) -> list[Path]:
    ranked = sorted(files, key=lambda path: (-image_pixel_count(path), path.name.lower()))
    return ranked[:limit]


def _load_cache() -> dict:
    data = load_json(config.CACHE_PATH, {"products": {}})
    if not isinstance(data, dict) or not isinstance(data.get("products"), dict):
        return {"products": {}}
    return data


def _needs_call(entry: dict | None, fingerprint: str, force: bool) -> bool:
    if force or not entry:
        return True
    return entry.get("fingerprint") != fingerprint


def _confirm(n_products: int, n_images: int, assume_yes: bool) -> bool:
    cost = config.estimate_cost_usd(n_products, n_images)
    LOG.info(
        "About to call vision on %d product%s using up to %d image%s. Estimated cost: ~$%.2f.",
        n_products,
        "" if n_products == 1 else "s",
        n_images,
        "" if n_images == 1 else "s",
        cost,
    )
    LOG.info(
        "Estimate assumes ~%d input tokens/image at $%.0f/MTok and ~%d output tokens/product at $%.0f/MTok. It is not a quote.",
        config.INPUT_TOKENS_PER_IMAGE,
        config.INPUT_USD_PER_MTOK,
        config.OUTPUT_TOKENS_PER_PRODUCT,
        config.OUTPUT_USD_PER_MTOK,
    )
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        LOG.error("Refusing to call the API without --yes when stdin is not a terminal.")
        return False
    answer = input("Continue? [y/N] ").strip().lower()
    if answer not in {"y", "yes"}:
        LOG.info("Aborted before any API call.")
        return False
    return True


def _fallback_listing(reason: str) -> dict[str, str]:
    LOG.warning("Using fallback listing (%s)", reason)
    return {
        "title": "Untitled product",
        "description": "Automatic description failed; please write one.",
        "category": "Misc",
        "condition": "Used",
        "notes": "needs review",
    }


def plan(*, force: bool = False) -> dict:
    """How many products still need a vision call, and a rough cost."""
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    cache = _load_cache()
    products = cache.get("products") if isinstance(cache.get("products"), dict) else {}
    product_count = 0
    pending = 0
    image_count = 0
    for folder in product_dirs():
        files = images_in(folder)
        if not files:
            continue
        product_count += 1
        fingerprint = folder_fingerprint(files)
        entry = products.get(folder.name)
        if _needs_call(entry if isinstance(entry, dict) else None, fingerprint, force):
            pending += 1
            image_count += len(representative_images(files, config.MAX_IMAGES_PER_PRODUCT_FOR_VISION))
    return {
        "products": product_count,
        "pending": pending,
        "images": image_count,
        "cost": round(config.estimate_cost_usd(pending, image_count), 2),
        "has_key": has_key,
    }


def run(args) -> int:
    folders = product_dirs()
    if not folders:
        LOG.error("No output/product-* folders. Run group first.")
        return 1

    cache = _load_cache()
    products: dict = cache["products"]
    pending: list[tuple[Path, list[Path], str]] = []

    for folder in folders:
        files = images_in(folder)
        if not files:
            LOG.warning("Skipping empty %s", folder.name)
            continue
        fingerprint = folder_fingerprint(files)
        entry = products.get(folder.name)
        if _needs_call(entry if isinstance(entry, dict) else None, fingerprint, args.force):
            pending.append((folder, files, fingerprint))
        else:
            LOG.info("Cache hit %s", folder.name)

    if args.limit is not None:
        if args.limit < 0:
            LOG.error("--limit must be >= 0")
            return 1
        if args.limit == 0:
            LOG.info("--limit 0; not calling the API.")
            return 0
        pending = pending[: args.limit]

    if not pending:
        LOG.info("Nothing to describe. Cache is up to date.")
        return 0

    image_budget = 0
    prepared: list[tuple[Path, list[Path], str, list[Path]]] = []
    for folder, files, fingerprint in pending:
        chosen = representative_images(files, config.MAX_IMAGES_PER_PRODUCT_FOR_VISION)
        prepared.append((folder, files, fingerprint, chosen))
        image_budget += len(chosen)

    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        LOG.error("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key.")
        return 1

    if not _confirm(len(prepared), image_budget, args.yes):
        return 1

    described = 0
    for index, (folder, _files, fingerprint, chosen) in enumerate(prepared):
        if index > 0 and config.DESCRIBE_PAUSE_SECONDS:
            time.sleep(config.DESCRIBE_PAUSE_SECONDS)
        LOG.info(
            "Describing %s (%d/%d) with %d image(s)",
            folder.name,
            index + 1,
            len(prepared),
            len(chosen),
        )
        encoded: list[bytes] = []
        for path in chosen:
            try:
                encoded.append(
                    jpeg_bytes_max_edge(
                        path,
                        config.VISION_IMAGE_MAX_EDGE,
                        quality=config.VISION_JPEG_QUALITY,
                    )
                )
            except ImageReadError as exc:
                LOG.warning("Could not read %s (%s)", path.name, exc)
        if not encoded:
            listing = _fallback_listing("no readable images")
            raw = None
        else:
            try:
                listing = describe_product(encoded, model=config.VISION_MODEL)
                raw = dict(listing)
                listing["notes"] = ""
            except InvalidListingError as exc:
                listing = _fallback_listing(str(exc))
                raw = {"error": str(exc)}
            except (anthropic.APIError, RuntimeError) as exc:
                LOG.error("API error on %s after retries: %s", folder.name, exc)
                LOG.error("Stopping. Re-run describe to continue; finished products stay cached.")
                return 1

        products[folder.name] = {
            "fingerprint": fingerprint,
            "title": listing["title"],
            "description": listing["description"],
            "category": listing["category"],
            "condition": listing["condition"],
            "notes": listing.get("notes", ""),
            "model": config.VISION_MODEL,
            "image_count": len(encoded),
            "described_at": datetime.now().isoformat(timespec="seconds"),
            "raw": raw,
        }
        cache["products"] = products
        atomic_write_json(config.CACHE_PATH, cache)
        described += 1
        LOG.info(
            "%s -> %s | %s | %s",
            folder.name,
            listing["category"],
            listing["condition"],
            listing["title"],
        )

    LOG.info("Described %d product(s). Cache: %s", described, config.CACHE_PATH.relative_to(config.ROOT))
    return 0
