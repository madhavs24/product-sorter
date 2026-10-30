# Product sorter

Standalone command-line tool that takes a mixed folder of product photos, groups the shots that belong to the same item, drafts a title / description / category / condition, and writes an editable manifest.

The same sorter runs in the browser, with no install: [product sorter](https://madhavs24.github.io/product-sorter/). Drop a photo folder, then download a zip. The photos are sorted on your computer and are not uploaded.

This folder is completely separate from the marketplace app. It does not import that code, and it never reads or writes the marketplace database (including Supabase). The only network call is the vision API, and only when you run `describe`.

The hand-off to a later import step is `output/manifest.json`, `output/products.csv`, and the image files under `output/product-XXX/`. The import script is not part of this tool.

## Setup

Requires Python 3.11 or newer.

```bash
cd ~/product-sorter
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set `ANTHROPIC_API_KEY`. You can also set `VISION_MODEL` (default `claude-sonnet-4-5`).

```bash
python sorter.py --help
```

## Run

Replace the dump path with your photo folder. The dump is only read. HEIC is converted to JPEG inside this project; originals are not modified, renamed, or deleted.

```bash
python sorter.py ui
```

That opens a page in your browser. Click the page and choose the photo folder in the Finder window. The page groups the photos, drafts a title and description for each product, and writes a `sorted` folder inside the photo folder you chose. The original photos are not changed.

You can do the same thing from the command line:

```bash
python sorter.py run ~/Desktop/photo-dump
```

That reads the folder, groups the photos, drafts a title and description for each product, and writes subfolders to both of these places:

- `~/product-sorter/output/sorted/01 Title/`
- `~/Desktop/photo-dump/sorted/01 Title/`

Each subfolder contains that product’s photos and a `listing.txt` (title, description, category, condition, and a blank price). The loose photos you started with stay where they are.

You can still run the steps one at a time:

```bash
python sorter.py ingest ~/Desktop/photo-dump
python sorter.py group
open output/contact-sheet.html   # review, fix in Finder
python sorter.py describe
python sorter.py export --into ~/Desktop/photo-dump/sorted
```

### 1. Ingest

```bash
python sorter.py ingest ~/Desktop/photo-dump
python sorter.py ingest ~/Desktop/photo-dump --force   # wipe working images and start over
```

What it does:

- Walks the dump, including subfolders.
- Copies each image into `working/images/` as an auto-oriented JPEG. iPhone HEIC/HEIF is supported.
- Skips non-images, empty files, and files that are not actually images.
- Drops exact duplicates (same file bytes).
- Writes a thumbnail per image under `working/thumbs/` and a catalog at `working/index.json`.

Safe to interrupt and re-run. Files already in the index are skipped. `--force` clears `working/images`, `working/thumbs`, and `working/index.json` only. It does not touch the dump or `output/`.

### 2. Group

```bash
python sorter.py group
python sorter.py group --threshold 120
python sorter.py group --dry-run
```

Photos are walked in the order they were taken. Capture time includes the camera’s millisecond fraction (`SubSecTimeOriginal`), so two shots in the same second stay in order. A new angle of the same item stays in the same product, including a sole, tag, lining, or a close-up of the fabric. A new product starts when the next photo is a different item. That includes another pair of shoes or another piece of clothing set down on the same table, not only a long pause. A pause of about **90 seconds** still separates products whose photos look alike (`PRODUCT_GAP_SECONDS` in `config.py`). A product never keeps more than **5** photos; a longer run is split at the weakest join (`MAX_PHOTOS_PER_PRODUCT`).

Shots with no capture time are still walked in filename order, spaced like a normal burst, so a color change can start a new product. A photo that cannot be read does not get glued to everything that follows.

Sorting a new folder does not reuse titles from the previous folder. A title is kept only when the photos in that product are unchanged.

```bash
python sorter.py group --threshold 120   # looser: wait 2 minutes before starting a new product
```

`--threshold` is that pause, in seconds. Higher keeps more angles together. Lower splits the shoot into more products.

`--dry-run` prints the clusters and writes nothing. A real run deletes and rebuilds `output/product-001/`, `output/product-002/`, … and `output/contact-sheet.html`. It does not delete `manifest.json`, `products.csv`, or the description cache.

The command prints how many products and images it found.

**Re-running `group` throws away Finder fixes.** Do that only when you want a fresh clustering.

### 3. Review the groups

Open the contact sheet:

```bash
open output/contact-sheet.html
```

Each box is one product. If a photo is in the wrong box, move the image file between the `output/product-XXX/` folders in Finder (drag it out of one folder into another). Create a new `output/product-XXX/` folder if a photo needs to be its own product. `describe` and `export` read whatever is in those folders; there is no separate "fix" command.

### 4. Describe

```bash
python sorter.py describe
python sorter.py describe --limit 3          # first 3 uncached products, for a cheap check
python sorter.py describe --yes              # skip the confirmation prompt
python sorter.py describe --force            # ignore the cache and call the API again
```

For each product folder the command sends at most 4 images, each downscaled so the long edge is at most 1024px. The model must return:

- a short title
- a one or two sentence description
- one category: Textbooks, Electronics, Furniture, Apparel, Misc, Services
- one condition: Brand New, Like New, Used

Those enums are enforced with a tool schema. If a reply still falls outside the lists after one corrective retry, the product is saved as category `Misc`, condition `Used`, notes `needs review`.

Before any API call it prints how many products and images it will send and a rough cost estimate, then asks you to confirm. API errors (rate limit, timeout, 5xx) retry with exponential backoff, up to 5 attempts, with a short pause between products. If a product still fails, the command stops. Products already described stay in `output/describe-cache.json`; run `describe` again to continue.

The cache key is the product folder name plus a fingerprint of the filenames and file sizes in that folder. Moving, adding, or removing a photo changes the fingerprint, so that product is sent again. Unchanged folders are not sent again.

### 5. Export

```bash
python sorter.py export
```

Writes:

- `output/manifest.json` — one object per product
- `output/products.csv` — the same rows, UTF-8 with a BOM so spreadsheet apps open it cleanly

Columns: `product_id`, `folder`, `image_files`, `title`, `description`, `category`, `condition`, `price`, `seller_email`, `notes`.

`folder` is relative to this project (`output/product-001`). In the CSV, `image_files` is a semicolon-separated list of filenames inside that folder. In the JSON file it is an array. `price` and `seller_email` are always blank — fill those in before import. `notes` is blank unless describe flagged the row.

Export works even if you have not run `describe`; listing fields are just empty. **Re-running export overwrites both files**, including any price or seller email you typed into them. Fill those in on the copy you intend to import, or fill them in after the last export.

## Layout

```
~/product-sorter/
  sorter.py            CLI
  config.py            categories, conditions, threshold, model
  .env                 API key (not committed)
  src/                 ingest, group, describe, export, vision
  working/             normalized JPEGs, thumbnails, index (generated)
  output/              product folders, contact sheet, cache, manifest (generated)
```

Logs go to the terminal and to `working/run.log`. `--verbose` prints per-file detail.

## Swapping the vision provider

`describe` calls a single function:

```python
describe_product(images: list[bytes], *, model: str) -> dict
```

defined in `src/vision.py`. It returns `title`, `description`, `category`, and `condition`. Replacing the body of that function is enough to point at OpenAI or another provider. Nothing else imports the Anthropic client.

## Tuning and odd files

- One bad file does not stop ingest. It is logged and skipped.
- Symlinks are skipped.
- Very large images (over 100 megapixels) are skipped rather than decoded.
- Photos with no capture time are grouped by how similar they look, instead of by time.
- A few thousand images is comfortable. The timed grouping walks the shoot in order.
- Two different items shot a few seconds apart can still share a folder if they look alike. Move the extra photo in Finder. A higher `--threshold` keeps more angles together; a lower one starts a new product sooner.
