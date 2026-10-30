"""Copy a photo dump into working/, normalized and de-duplicated.

The dump folder is read-only. Exact duplicates (same file bytes) collapse
onto one working JPEG keyed by SHA-1 of those bytes.
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime
from pathlib import Path

import config
from src.imaging import ImageReadError, load_oriented, make_thumbnail, perceptual_hashes, save_jpeg
from src.utils import LOG, atomic_write_json, is_inside, load_json

INDEX_VERSION = 1


def _sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files_present(item: dict) -> bool:
    dst = item.get("dst")
    thumb = item.get("thumb")
    if not dst or not thumb:
        return False
    return (config.ROOT / dst).is_file() and (config.ROOT / thumb).is_file()


def _empty_index() -> dict:
    return {"version": INDEX_VERSION, "items": []}


def _load_index() -> dict:
    data = load_json(config.INDEX_PATH, _empty_index())
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        LOG.warning("Index was unusable; starting fresh")
        return _empty_index()
    return data


def _iter_files(dump: Path):
    # `sorted/` is this tool's output inside the photo folder. Leave it alone
    # so a second run does not treat those copies as new photos.
    own_output = (dump / "sorted").resolve()
    for path in dump.rglob("*"):
        if path.is_symlink():
            LOG.debug("skip symlink %s", path)
            continue
        if not path.is_file():
            continue
        try:
            path.resolve().relative_to(own_output)
        except ValueError:
            yield path
        else:
            LOG.debug("skip sorted output %s", path)


def run(args) -> int:
    dump = Path(args.dump_folder).expanduser().resolve()
    if not dump.is_dir():
        LOG.error("Dump folder does not exist or is not a directory: %s", dump)
        return 1
    if is_inside(dump, config.WORKING_DIR) or is_inside(dump, config.OUTPUT_DIR):
        LOG.error("Refusing to ingest from working/ or output/. Point at the original dump.")
        return 1

    if args.force:
        LOG.info("Clearing normalized images and the index")
        for path in (config.IMAGES_DIR, config.THUMBS_DIR):
            if path.exists():
                shutil.rmtree(path)
        if config.INDEX_PATH.exists():
            config.INDEX_PATH.unlink()

    config.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    config.THUMBS_DIR.mkdir(parents=True, exist_ok=True)

    index = _load_index()
    known = {item["id"]: item for item in index["items"] if isinstance(item, dict) and "id" in item}
    seen_this_run: set[str] = set()

    kept = 0
    already = 0
    duplicates = 0
    skipped = 0
    scanned = 0

    files = list(_iter_files(dump))
    LOG.info("Scanning %d files in %s", len(files), dump)

    for path in files:
        scanned += 1
        if path.suffix.lower() not in config.IMAGE_EXTENSIONS:
            skipped += 1
            LOG.debug("skip non-image %s", path)
            continue
        if path.stat().st_size == 0:
            skipped += 1
            LOG.warning("skip empty file %s", path)
            continue

        try:
            file_id = _sha1_file(path)
        except OSError as exc:
            skipped += 1
            LOG.warning("skip unreadable %s (%s)", path, exc)
            continue

        if file_id in seen_this_run:
            duplicates += 1
            LOG.debug("exact duplicate %s", path)
            continue
        if file_id in known and _files_present(known[file_id]):
            already += 1
            seen_this_run.add(file_id)
            LOG.debug("already ingested %s", path)
            continue
        if file_id in known:
            LOG.info("Rebuilding missing working files for %s", path.name)
            index["items"] = [item for item in index["items"] if item.get("id") != file_id]

        dest = config.IMAGES_DIR / f"{file_id}.jpg"
        thumb_path = config.THUMBS_DIR / f"{file_id}.jpg"
        try:
            image, captured = load_oriented(path)
            phash, dhash = perceptual_hashes(image)
            save_jpeg(image, dest, quality=config.JPEG_QUALITY)
            thumb = make_thumbnail(image, config.THUMB_MAX_EDGE)
            save_jpeg(thumb, thumb_path, quality=config.THUMB_JPEG_QUALITY)
        except ImageReadError as exc:
            skipped += 1
            LOG.warning("skip undecodable %s (%s)", path, exc)
            continue
        except OSError as exc:
            skipped += 1
            LOG.warning("skip %s (%s)", path, exc)
            continue

        item = {
            "id": file_id,
            "src": str(path),
            "original_name": path.name,
            "dst": dest.relative_to(config.ROOT).as_posix(),
            "thumb": thumb_path.relative_to(config.ROOT).as_posix(),
            "phash": phash,
            "dhash": dhash,
            "exif_time": captured.isoformat(timespec="microseconds") if captured else None,
            "width": image.width,
            "height": image.height,
        }
        index["items"].append(item)
        known[file_id] = item
        seen_this_run.add(file_id)
        kept += 1

        if kept % 25 == 0:
            atomic_write_json(config.INDEX_PATH, index)
            LOG.info("Ingested %d new images so far (%d/%d files scanned)", kept, scanned, len(files))

    index["updated_at"] = datetime.now().isoformat(timespec="seconds")
    atomic_write_json(config.INDEX_PATH, index)
    LOG.info(
        "Ingest done: %d new, %d already present, %d exact duplicates, %d skipped",
        kept,
        already,
        duplicates,
        skipped,
    )
    return 0
