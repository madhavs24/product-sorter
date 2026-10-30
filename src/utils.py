"""Logging, atomic writes, and small shared helpers."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import config

LOG = logging.getLogger("sorter")


def setup_logging(verbose: bool) -> None:
    config.WORKING_DIR.mkdir(parents=True, exist_ok=True)
    LOG.setLevel(logging.DEBUG)
    LOG.handlers.clear()
    LOG.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    stream = logging.StreamHandler()
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream.setFormatter(formatter)

    file_handler = logging.FileHandler(config.LOG_PATH, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    LOG.addHandler(stream)
    LOG.addHandler(file_handler)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        LOG.warning("Could not parse %s; starting fresh", path.name)
        return default


def rel_to_root(path: Path) -> str:
    return path.resolve().relative_to(config.ROOT).as_posix()


def is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True
