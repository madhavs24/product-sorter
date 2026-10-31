"""Tunable settings for the product sorter.

Categories, conditions, the grouping threshold, and the vision model name
live here (the model can also be overridden with VISION_MODEL in .env).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
WORKING_DIR = ROOT / "working"
OUTPUT_DIR = ROOT / "output"
IMAGES_DIR = WORKING_DIR / "images"
THUMBS_DIR = WORKING_DIR / "thumbs"
INDEX_PATH = WORKING_DIR / "index.json"
LOG_PATH = WORKING_DIR / "run.log"
CACHE_PATH = OUTPUT_DIR / "describe-cache.json"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
CSV_PATH = OUTPUT_DIR / "products.csv"
CONTACT_SHEET_PATH = OUTPUT_DIR / "contact-sheet.html"

load_dotenv(ROOT / ".env")

ALLOWED_CATEGORIES = [
    "Textbooks",
    "Electronics",
    "Furniture",
    "Apparel",
    "Misc",
    "Services",
]
ALLOWED_CONDITIONS = ["Brand New", "Like New", "Used"]

# A pause at least this long starts a new product. Different angles of the
# same item are usually shot within a minute, so they stay together.
# Override with `group --threshold` (value is seconds). Higher = looser.
PRODUCT_GAP_SECONDS = 90.0

# Used only when photos have no capture time. Hamming distance on pHash.
HASH_ONLY_THRESHOLD = 18.0
DHASH_WEIGHT = 0.25

VISION_MODEL = os.getenv("VISION_MODEL", "claude-sonnet-4-5")
MAX_IMAGES_PER_PRODUCT_FOR_VISION = 4
VISION_IMAGE_MAX_EDGE = 1024
THUMB_MAX_EDGE = 512
JPEG_QUALITY = 90
THUMB_JPEG_QUALITY = 80
VISION_JPEG_QUALITY = 85

# Rough public-price estimate printed before `describe`. Not a bill.
# ~1,500 input tokens per downscaled image, ~250 output tokens per product.
INPUT_USD_PER_MTOK = 3.0
OUTPUT_USD_PER_MTOK = 15.0
INPUT_TOKENS_PER_IMAGE = 1500
OUTPUT_TOKENS_PER_PRODUCT = 250

# Pause between vision calls so a large dump does not hammer the API.
DESCRIBE_PAUSE_SECONDS = 0.5

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".webp",
    ".tif",
    ".tiff",
    ".bmp",
}

MANIFEST_FIELDS = [
    "product_id",
    "folder",
    "image_files",
    "title",
    "description",
    "category",
    "condition",
    "price",
    "seller_email",
    "notes",
]


def estimate_cost_usd(n_products: int, n_images: int) -> float:
    input_cost = n_images * INPUT_TOKENS_PER_IMAGE * INPUT_USD_PER_MTOK
    output_cost = n_products * OUTPUT_TOKENS_PER_PRODUCT * OUTPUT_USD_PER_MTOK
    return (input_cost + output_cost) / 1_000_000
