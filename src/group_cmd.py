"""Cluster working images into one folder per product.

Photos are walked in capture-time order. A new angle of the same item stays
in the current product. A new product starts after a pause (default 90s) or
when the colors clearly change, which is someone picking up a different item.
The dump is never touched. Re-running deletes and rebuilds output/product-*/
and the contact sheet only.
"""

from __future__ import annotations

import html
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image
from sklearn.cluster import AgglomerativeClustering

import config
from src.utils import LOG, atomic_write_text, load_json

PRODUCT_PREFIX = "product-"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _safe_stem(name: str) -> str:
    stem = Path(name).stem
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)
    cleaned = cleaned.strip("_") or "image"
    return cleaned[:40]


def load_items() -> list[dict]:
    data = load_json(config.INDEX_PATH, None)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return []
    items = []
    for item in data["items"]:
        if not isinstance(item, dict):
            continue
        if not item.get("id") or not item.get("phash") or not item.get("dst"):
            LOG.warning("Skipping incomplete index entry")
            continue
        dst = config.ROOT / item["dst"]
        if not dst.is_file():
            LOG.warning("Missing working image for %s; skipping", item.get("original_name"))
            continue
        items.append(item)
    items.sort(key=lambda item: (item.get("exif_time") or "", item.get("original_name") or "", item["id"]))
    return items


def _shot_cues(item: dict) -> tuple[np.ndarray, float] | None:
    """Center color, and how different the border is from the center.

    A high border gap means the object is sitting on a table, not filling the
    frame. A close-up of a sole, tag, or lining has a small gap.
    """
    cached = item.get("_shot_cues")
    if cached is not None:
        return cached
    thumb = item.get("thumb")
    if not thumb:
        return None
    path = config.ROOT / thumb
    if not path.is_file():
        return None
    try:
        with Image.open(path) as image:
            rgb = image.convert("RGB").resize((80, 80))
    except OSError:
        return None
    pixels = np.asarray(rgb).astype(np.float32) / 255.0
    center = pixels[16:64, 16:64]
    bins = []
    for channel in range(3):
        hist, _edges = np.histogram(center[:, :, channel], bins=8, range=(0, 1), density=True)
        bins.append(hist)
    vector = np.concatenate(bins)
    vector = vector / (np.linalg.norm(vector) + 1e-9)
    border = np.concatenate(
        [
            pixels[:8].reshape(-1, 3),
            pixels[-8:].reshape(-1, 3),
            pixels[:, :8].reshape(-1, 3),
            pixels[:, -8:].reshape(-1, 3),
        ]
    )
    border_delta = float(np.linalg.norm(border.mean(axis=0) - center.reshape(-1, 3).mean(axis=0)))
    item["_shot_cues"] = (vector, border_delta)
    return item["_shot_cues"]


def _color_signature(item: dict) -> tuple[np.ndarray, np.ndarray, float] | None:
    """Return a color histogram, mean RGB, and how much of the center is blue."""
    thumb = item.get("thumb")
    if not thumb:
        return None
    path = config.ROOT / thumb
    if not path.is_file():
        return None
    try:
        with Image.open(path) as image:
            rgb = image.convert("RGB").resize((48, 48))
    except OSError:
        return None
    pixels = np.asarray(rgb).astype(np.float32) / 255.0
    bins = []
    for channel in range(3):
        hist, _edges = np.histogram(pixels[:, :, channel], bins=8, range=(0, 1), density=True)
        bins.append(hist)
    vector = np.concatenate(bins)
    vector = vector / (np.linalg.norm(vector) + 1e-9)
    center = pixels[12:36, 12:36]
    red, green, blue = center[:, :, 0], center[:, :, 1], center[:, :, 2]
    blue_fraction = float(((blue > red + 0.08) & (blue > green + 0.02) & (blue > 0.25)).mean())
    return vector, pixels.reshape(-1, 3).mean(axis=0), blue_fraction


def _starts_new_product(
    previous: dict,
    current: dict,
    gap_seconds: float,
    pause_seconds: float,
) -> bool:
    """True when `current` is a different item than `previous`.

    Near-duplicate frames always stay together. A long pause starts a new
    product. A shorter gap starts one only when the colors actually change,
    so a new angle of the same object is not split off.
    """
    try:
        phash_distance = imagehash.hex_to_hash(previous["phash"]) - imagehash.hex_to_hash(current["phash"])
    except (ValueError, TypeError, KeyError):
        phash_distance = 64
    if phash_distance <= 12 and gap_seconds <= 180:
        return False

    # A photo we cannot read must not glue the rest of the roll together.
    previous_cues = _shot_cues(previous)
    current_cues = _shot_cues(current)
    if previous_cues is None or current_cues is None:
        return gap_seconds >= 8

    # Clothes and shoes are often shot on the same table, a few seconds apart,
    # so the whole frame looks alike. Compare the center of the photo, and
    # treat a wide shot (table around the object) as a new product sooner
    # than a close-up of the sole, tag, or lining.
    if previous_cues is not None and current_cues is not None:
        center_cosine = float(np.dot(previous_cues[0], current_cues[0]))
        border_delta = current_cues[1]
        zoomed_in = previous_cues[1] - border_delta >= 0.15
        medium_gap = max(30.0, pause_seconds * 0.44)
        wide_gap = max(18.0, pause_seconds * 0.29)
        # A sole, tag, or lining fills the frame and stays with the item.
        # A different color a few seconds later, such as a black strip after
        # a white one, does not: that change is too fast to be another angle.
        if gap_seconds < 40 and border_delta < 0.15 and (center_cosine >= 0.70 or gap_seconds >= 18):
            return False
        # A later close-up of the same fabric, such as towel texture.
        if medium_gap <= gap_seconds < pause_seconds and border_delta < 0.12 and center_cosine >= 0.45:
            return False
        # A real pause is a new product, including another garment. The same
        # jacket or blanket can still match very closely after a slow angle.
        if gap_seconds >= pause_seconds and center_cosine < 0.93:
            return True
        if gap_seconds >= medium_gap and center_cosine < 0.90:
            return True
        # Another pair on the table. Clothing angles sit closer to the edge
        # of the frame than a shoe, so the border gap has to be clearly large.
        if gap_seconds >= wide_gap and border_delta >= 0.51 and center_cosine < 0.90:
            return True
        # Same setup, different product, a few seconds later. Zooming into
        # the sole or the fabric drops the border, so that stays with the item.
        if (
            8 <= gap_seconds < 18
            and border_delta < 0.15
            and center_cosine < 0.65
            and not zoomed_in
        ):
            return True

    prev_sig = _color_signature(previous)
    curr_sig = _color_signature(current)
    if prev_sig is None or curr_sig is None:
        return False
    cosine = float(np.dot(prev_sig[0], curr_sig[0]))
    rgb_delta = float(np.linalg.norm(prev_sig[1] - curr_sig[1]))
    # A clearly blue item next to one that is not blue is a different product
    # (a blue bag after a grey one). A close-up that is still the same item,
    # such as a blue shoe sole, keeps a similar overall color and stays put.
    blue_delta = abs(prev_sig[2] - curr_sig[2])
    if (
        10 <= gap_seconds < 70
        and blue_delta >= 0.35
        and min(prev_sig[2], curr_sig[2]) < 0.15
        and cosine < 0.85
    ):
        return True

    # Obvious swap to a different item, even if it happened quickly.
    if 10 <= gap_seconds < 20 and cosine < 0.74 and rgb_delta >= 0.10:
        return True
    # A different item a bit later, while still ignoring a slow angle change.
    if 20 <= gap_seconds < 70 and (cosine < 0.70 or (cosine < 0.78 and rgb_delta >= 0.09)):
        return True
    if cosine < 0.55 and gap_seconds >= 10:
        return True
    return False


def _matches_earlier_shot(group: list[dict], item: dict) -> bool:
    """True when a close-up matches an earlier photo already in this product."""
    cues = _shot_cues(item)
    if cues is None or cues[1] >= 0.35:
        return False
    for member in group:
        other = _shot_cues(member)
        if other is None:
            continue
        if float(np.dot(other[0], cues[0])) >= 0.86:
            return True
    return False


def _cluster_by_time(items: list[dict], pause_seconds: float) -> list[list[dict]]:
    ordered = sorted(
        items,
        key=lambda item: (item.get("exif_time") or "", item.get("original_name") or "", item["id"]),
    )
    groups: list[list[dict]] = [[ordered[0]]]
    previous_time = _parse_time(ordered[0].get("exif_time"))
    for item in ordered[1:]:
        current_time = _parse_time(item.get("exif_time"))
        if previous_time is None or current_time is None:
            gap = pause_seconds
        else:
            gap = abs((current_time - previous_time).total_seconds())
        if _starts_new_product(groups[-1][-1], item, gap, pause_seconds):
            # A sole or tag often does not look like the frame just before it,
            # but it still matches an earlier angle of the same item.
            if gap <= 40 and _matches_earlier_shot(groups[-1], item):
                groups[-1].append(item)
            else:
                groups.append([item])
        else:
            groups[-1].append(item)
        if current_time is not None:
            previous_time = current_time
    return _attach_leading_details(groups)


def _attach_leading_details(groups: list[list[dict]]) -> list[list[dict]]:
    """A lining or tag shot just before the main photo belongs with that product."""
    if len(groups) < 2:
        return groups
    kept: list[list[dict]] = []
    index = 0
    while index < len(groups):
        group = groups[index]
        if len(group) == 1 and index + 1 < len(groups):
            cues = _shot_cues(group[0])
            current = _parse_time(group[0].get("exif_time"))
            nxt = _parse_time(groups[index + 1][0].get("exif_time"))
            gap = 999.0
            if current is not None and nxt is not None:
                gap = abs((nxt - current).total_seconds())
            next_cues = _shot_cues(groups[index + 1][0]) if cues is not None else None
            # Only a tight detail followed by a wide shot of the product,
            # such as fur lining then the pair of boots. A close-up that is
            # its own item, such as a pack of pods, is left alone.
            if (
                cues is not None
                and next_cues is not None
                and cues[1] < 0.25
                and next_cues[1] >= 0.50
                and gap <= 120
            ):
                groups[index + 1] = group + groups[index + 1]
                index += 1
                continue
        kept.append(group)
        index += 1
    return kept


def _cluster_by_hash(items: list[dict], threshold: float) -> list[list[dict]]:
    if len(items) == 1:
        return [list(items)]
    matrix = np.zeros((len(items), len(items)), dtype=np.float64)
    hashes = []
    for item in items:
        phash = imagehash.hex_to_hash(item["phash"])
        dhash = imagehash.hex_to_hash(item["dhash"]) if item.get("dhash") else phash
        hashes.append((phash, dhash))
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            visual = (hashes[i][0] - hashes[j][0]) + config.DHASH_WEIGHT * (hashes[i][1] - hashes[j][1])
            matrix[i, j] = matrix[j, i] = float(visual)
    model = AgglomerativeClustering(
        metric="precomputed",
        linkage="average",
        distance_threshold=threshold,
        n_clusters=None,
        compute_full_tree=True,
    )
    labels = model.fit_predict(matrix)
    grouped: dict[int, list[dict]] = {}
    for item, label in zip(items, labels):
        grouped.setdefault(int(label), []).append(item)
    return list(grouped.values())


def cluster_items(items: list[dict], pause_seconds: float) -> list[list[dict]]:
    """Group photos of the same product.

    `pause_seconds` is how long a gap between shots must be before it starts
    a new product. Shots with no capture time fall back to perceptual-hash
    clustering.
    """
    if not items:
        return []
    if len(items) == 1:
        return [list(items)]

    timed = [item for item in items if _parse_time(item.get("exif_time"))]
    untimed = [item for item in items if not _parse_time(item.get("exif_time"))]
    clusters: list[list[dict]] = []
    if len(timed) >= 2:
        clusters.extend(_cluster_by_time(timed, pause_seconds))
    elif timed:
        clusters.append(timed)
    # Photos with no clock still have a shooting order: the filename order.
    # Space them like a normal burst so a color change can start a new product
    # instead of letting a hash cluster glue the whole dump into one.
    if len(untimed) >= 2:
        ordered = sorted(untimed, key=lambda item: (item.get("original_name") or "", item["id"]))
        base = datetime(2000, 1, 1)
        spaced = []
        for index, item in enumerate(ordered):
            copy = dict(item)
            copy["exif_time"] = (base + timedelta(seconds=12 * index)).isoformat(timespec="seconds")
            spaced.append(copy)
        clusters.extend(_cluster_by_time(spaced, pause_seconds))
    elif untimed:
        clusters.append(untimed)

    for cluster in clusters:
        cluster.sort(key=lambda item: (item.get("exif_time") or "", item.get("original_name") or "", item["id"]))
    clusters.sort(key=lambda cluster: (cluster[0].get("exif_time") or "9999", cluster[0]["id"]))
    return clusters


def _clear_product_dirs() -> None:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in config.OUTPUT_DIR.iterdir():
        if path.is_dir() and path.name.startswith(PRODUCT_PREFIX):
            shutil.rmtree(path)
    if config.CONTACT_SHEET_PATH.exists():
        config.CONTACT_SHEET_PATH.unlink()


def _copy_cluster(product_id: str, cluster: list[dict]) -> list[dict]:
    folder = config.OUTPUT_DIR / product_id
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    used_names: set[str] = set()
    for index, item in enumerate(cluster, start=1):
        stem = _safe_stem(item.get("original_name") or item["id"])
        filename = f"{index:02d}-{stem}-{item['id'][:8]}.jpg"
        if filename in used_names:
            filename = f"{index:02d}-{item['id'][:12]}.jpg"
        used_names.add(filename)
        dest = folder / filename
        shutil.copy2(config.ROOT / item["dst"], dest)
        thumb = item.get("thumb")
        written.append(
            {
                "filename": filename,
                "thumb_rel": None if not thumb else Path("..") / thumb,
                "src_name": item.get("original_name") or filename,
            }
        )
    return written


def render_contact_sheet(rows: list[dict], threshold: float) -> str:
    sections = []
    for row in rows:
        cards = []
        for image in row["images"]:
            src = html.escape(image["src"], quote=True)
            alt = html.escape(image["label"], quote=True)
            cards.append(f'<figure><img src="{src}" alt="{alt}"><figcaption>{alt}</figcaption></figure>')
        title = html.escape(row["product_id"])
        sections.append(
            "<section class=\"product\">"
            f"<h2>{title} <span>{len(row['images'])} image{'s' if len(row['images']) != 1 else ''}</span></h2>"
            f"<div class=\"row\">{''.join(cards)}</div>"
            "</section>"
        )
    body = "\n".join(sections) or "<p>No products.</p>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Product contact sheet</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 24px; background: #f4f3ef; color: #1c1c1c; }}
  header {{ margin-bottom: 20px; }}
  h1 {{ font-size: 22px; margin: 0 0 8px; }}
  p {{ margin: 0; color: #444; max-width: 46rem; line-height: 1.4; }}
  .product {{ background: #fff; border: 1px solid #e2e0d8; border-radius: 10px; padding: 16px; margin-bottom: 16px; }}
  h2 {{ font-size: 16px; margin: 0 0 12px; }}
  h2 span {{ font-weight: 400; color: #666; }}
  .row {{ display: flex; flex-wrap: wrap; gap: 10px; }}
  figure {{ margin: 0; width: 160px; }}
  img {{ width: 160px; height: 160px; object-fit: cover; background: #eee; border-radius: 6px; display: block; }}
  figcaption {{ font-size: 11px; color: #555; margin-top: 4px; word-break: break-all; }}
</style>
</head>
<body>
<header>
  <h1>Contact sheet</h1>
  <p>{len(rows)} products. Different angles of one item, including soles, tags,
  and linings, are kept together. A new box starts when the next photo is a
  different item, including another piece of clothing or another pair of shoes
  on the same table. If a photo is in the wrong box, move the image file
  between the <code>output/product-XXX</code>
  folders in Finder. Do not re-run <code>group</code> after fixing — it
  rebuilds these folders.</p>
</header>
{body}
</body>
</html>
"""


def _product_id(index: int, count: int) -> str:
    width = max(3, len(str(count)))
    return f"{PRODUCT_PREFIX}{index:0{width}d}"


def _print_summary(clusters: list[list[dict]], threshold: float, dry_run: bool) -> None:
    image_count = sum(len(cluster) for cluster in clusters)
    suffix = " (dry run, nothing written)" if dry_run else ""
    LOG.info(
        "Found %d products across %d images (new product after a %g second pause)%s",
        len(clusters),
        image_count,
        threshold,
        suffix,
    )
    for index, cluster in enumerate(clusters, start=1):
        LOG.info(
            "  %s  %d image%s",
            _product_id(index, len(clusters)),
            len(cluster),
            "" if len(cluster) == 1 else "s",
        )


def run(args) -> int:
    threshold = config.PRODUCT_GAP_SECONDS if args.threshold is None else float(args.threshold)
    if threshold < 0:
        LOG.error("--threshold must be >= 0")
        return 1

    items = load_items()
    if not items:
        LOG.error("No images in %s. Run ingest first.", config.INDEX_PATH.relative_to(config.ROOT))
        return 1

    LOG.info(
        "Grouping %d images. A different angle stays with the product; a new product starts after a %g second pause.",
        len(items),
        threshold,
    )
    try:
        clusters = cluster_items(items, threshold)
    except ValueError as exc:
        LOG.error("%s", exc)
        return 1

    _print_summary(clusters, threshold, args.dry_run)
    if args.dry_run:
        return 0

    LOG.info("Rebuilding output/product-* folders")
    _clear_product_dirs()
    rows = []
    for index, cluster in enumerate(clusters, start=1):
        product_id = _product_id(index, len(clusters))
        copied = _copy_cluster(product_id, cluster)
        images = []
        for image in copied:
            if image["thumb_rel"] is not None:
                src = image["thumb_rel"].as_posix()
            else:
                src = f"{product_id}/{image['filename']}"
            images.append({"src": src, "label": image["filename"]})
        rows.append({"product_id": product_id, "images": images})

    atomic_write_text(config.CONTACT_SHEET_PATH, render_contact_sheet(rows, threshold))
    LOG.info("Wrote %s", config.CONTACT_SHEET_PATH.relative_to(config.ROOT))
    LOG.info("Review it, then move files between product folders if a group is wrong.")
    return 0
