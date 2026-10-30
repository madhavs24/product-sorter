"""Load, orient, thumbnail, and hash product photos.

Originals are never written. Callers pass a destination path when they want
a normalized JPEG.
"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import imagehash
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

import config

register_heif_opener()

# Refuse absurd files instead of exhausting memory. Caught by ingest.
Image.MAX_IMAGE_PIXELS = 100_000_000

EXIF_DATETIME_ORIGINAL = 36867
EXIF_DATETIME = 306
EXIF_IFD = 0x8769
SUBSEC_ORIGINAL = 37521
SUBSEC_DIGITIZED = 37522
EXIF_FORMATS = ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S")


class ImageReadError(Exception):
    """A file looked like an image but could not be decoded."""


def to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image
    if image.mode == "RGBA":
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[-1])
        return background
    return image.convert("RGB")


def exif_datetime(image: Image.Image) -> datetime | None:
    """Capture time, including the camera's sub-second fraction when it has one.

    Phone photos store that fraction as milliseconds (SubSecTimeOriginal).
    It is kept so two shots in the same second are not treated as tied.
    """
    exif = image.getexif()
    if not exif:
        return None
    try:
        ifd = exif.get_ifd(EXIF_IFD)
    except (KeyError, AttributeError):
        ifd = {}
    raw = (
        ifd.get(EXIF_DATETIME_ORIGINAL)
        or exif.get(EXIF_DATETIME_ORIGINAL)
        or ifd.get(EXIF_DATETIME)
        or exif.get(EXIF_DATETIME)
    )
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    captured = None
    for fmt in EXIF_FORMATS:
        try:
            captured = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if captured is None:
        return None
    subsec = ifd.get(SUBSEC_ORIGINAL) or ifd.get(SUBSEC_DIGITIZED) or ""
    subsec = str(subsec).strip()
    if subsec.isdigit():
        captured = captured.replace(microsecond=int(subsec.ljust(6, "0")[:6]))
    return captured


def load_oriented(path: Path) -> tuple[Image.Image, datetime | None]:
    """Open an image, apply EXIF orientation, and return RGB plus capture time.

    Capture time is read before orientation is baked in, because the saved
    JPEG does not need to keep the original EXIF block.
    """
    try:
        with Image.open(path) as image:
            image.load()
            captured = exif_datetime(image)
            oriented = ImageOps.exif_transpose(image)
            if oriented is None:
                oriented = image
            rgb = to_rgb(oriented)
            return rgb.copy(), captured
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as exc:
        raise ImageReadError(str(exc)) from exc


def save_jpeg(image: Image.Image, path: Path, *, quality: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="JPEG", quality=quality, optimize=True)


def make_thumbnail(image: Image.Image, max_edge: int) -> Image.Image:
    thumb = image.copy()
    thumb.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    return thumb


def perceptual_hashes(image: Image.Image) -> tuple[str, str]:
    """Return hex pHash and dHash. Computed on a small copy to stay fast."""
    sample = make_thumbnail(image, config.THUMB_MAX_EDGE)
    phash = str(imagehash.phash(sample))
    dhash = str(imagehash.dhash(sample))
    return phash, dhash


def jpeg_bytes_max_edge(path: Path, max_edge: int, *, quality: int) -> bytes:
    image, _captured = load_oriented(path)
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def image_pixel_count(path: Path) -> int:
    try:
        with Image.open(path) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        return 0
    return int(width) * int(height)
