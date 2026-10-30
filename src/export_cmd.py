"""Write manifest.json, products.csv, and one folder per product.

Each product folder gets a listing.txt. A `sorted/` folder of plainly named
subfolders is also written so the photos and the product info sit together.
`describe` is optional. Products that were not described export with blank
listing fields so you can still edit them by hand.
"""

from __future__ import annotations

import csv
import io
import shutil
from pathlib import Path

import config
from src.describe_cmd import folder_fingerprint, images_in, product_dirs
from src.utils import LOG, atomic_write_json, atomic_write_text, load_json, rel_to_root


def _cache_entry(cache: dict, product_id: str, fingerprint: str) -> dict:
    """Return a saved listing only when it was written for these exact photos.

    Product ids are reused every time a new folder is sorted. Without the
    fingerprint check, product-001 of a new batch inherits the previous
    batch's title.
    """
    products = cache.get("products") if isinstance(cache, dict) else None
    if not isinstance(products, dict):
        return {}
    entry = products.get(product_id)
    if not isinstance(entry, dict):
        return {}
    if entry.get("fingerprint") != fingerprint:
        LOG.info("Photos in %s changed, so the previous title was not reused", product_id)
        return {}
    return entry


def _row(folder, files: list, entry: dict) -> dict[str, str]:
    names = [path.name for path in files]
    return {
        "product_id": folder.name,
        "folder": rel_to_root(folder),
        "image_files": names,
        "title": str(entry.get("title") or ""),
        "description": str(entry.get("description") or ""),
        "category": str(entry.get("category") or ""),
        "condition": str(entry.get("condition") or ""),
        "price": "",
        "seller_email": "",
        "notes": str(entry.get("notes") or ""),
    }


def _csv_text(rows: list[dict]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=config.MANIFEST_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        flat = dict(row)
        files = flat["image_files"]
        flat["image_files"] = "; ".join(files) if isinstance(files, list) else str(files)
        writer.writerow(flat)
    return buffer.getvalue()


def listing_text(row: dict) -> str:
    lines = [
        f"Title: {row['title']}",
        f"Category: {row['category']}",
        f"Condition: {row['condition']}",
        f"Price: {row['price']}",
        f"Seller email: {row['seller_email']}",
        "",
        row["description"] or "(no description yet)",
    ]
    if row.get("notes"):
        lines.extend(["", f"Notes: {row['notes']}"])
    lines.extend(["", "Photos:"])
    lines.extend(f"- {name}" for name in row["image_files"])
    return "\n".join(lines) + "\n"


def _folder_name(index: int, title: str, product_id: str) -> str:
    label = title.strip() or product_id
    cleaned = "".join(ch if ch.isalnum() or ch in " -" else " " for ch in label)
    cleaned = " ".join(cleaned.split()) or product_id
    return f"{index:02d} {cleaned}"[:70]


def publish_sorted(rows: list[dict], dest: Path) -> None:
    """Copy each product into dest/01 Title/ with its photos and listing.txt."""
    dest.mkdir(parents=True, exist_ok=True)
    for child in dest.iterdir():
        if child.is_dir() and len(child.name) >= 3 and child.name[:2].isdigit() and child.name[2] == " ":
            shutil.rmtree(child)
    for index, row in enumerate(rows, start=1):
        folder = dest / _folder_name(index, row["title"], row["product_id"])
        folder.mkdir()
        source = config.ROOT / row["folder"]
        for name in row["image_files"]:
            shutil.copy2(source / name, folder / name)
        atomic_write_text(folder / "listing.txt", listing_text(row))
    LOG.info("Wrote %d product folder(s) to %s", len(rows), dest)


def write_listing_files(rows: list[dict]) -> None:
    for row in rows:
        if not row["title"]:
            continue
        folder = config.ROOT / row["folder"]
        atomic_write_text(folder / "listing.txt", listing_text(row))


def run(args) -> int:
    folders = product_dirs()
    if not folders:
        LOG.error("No output/product-* folders. Run group first.")
        return 1

    cache = load_json(config.CACHE_PATH, {"products": {}})
    rows = []
    missing = 0
    for folder in folders:
        files = images_in(folder)
        if not files:
            LOG.warning("Skipping empty %s", folder.name)
            continue
        entry = _cache_entry(cache, folder.name, folder_fingerprint(files))
        if not entry.get("title"):
            missing += 1
        rows.append(_row(folder, files, entry))

    if not rows:
        LOG.error("Product folders exist but none contain images.")
        return 1

    atomic_write_json(config.MANIFEST_PATH, rows)
    # BOM so Excel treats the file as UTF-8.
    atomic_write_text(config.CSV_PATH, "\ufeff" + _csv_text(rows))
    write_listing_files(rows)
    sorted_dir = config.OUTPUT_DIR / "sorted"
    publish_sorted(rows, sorted_dir)
    extra = getattr(args, "publish_into", None)
    if extra:
        publish_sorted(rows, Path(extra).expanduser())
    LOG.info("Wrote %d product(s) to %s", len(rows), config.MANIFEST_PATH.relative_to(config.ROOT))
    LOG.info("Wrote %s", config.CSV_PATH.relative_to(config.ROOT))
    if missing:
        LOG.info("%d product(s) have no description yet. Run describe, then export again.", missing)
    LOG.info("price and seller_email are blank on purpose. Re-running export overwrites both files.")
    return 0
