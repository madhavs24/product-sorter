#!/usr/bin/env python3
"""Group a dump of product photos and draft a marketplace manifest.

This tool is standalone. It never reads or writes the marketplace app or
its database. See README.md for the full ingest → group → describe → export
sequence.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from src import describe_cmd, export_cmd, group_cmd, ingest_cmd, run_cmd, ui_cmd
from src.utils import LOG, setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python sorter.py",
        description="Group product photos, draft listings, and export a manifest.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Log each file, not just summaries")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="Copy and normalize images from a dump folder")
    ingest.add_argument("dump_folder", type=Path, help="Folder of mixed product photos (read-only)")
    ingest.add_argument("--force", action="store_true", help="Delete working/ and ingest from scratch")
    ingest.set_defaults(func=ingest_cmd.run)

    group = sub.add_parser("group", help="Cluster images into output/product-XXX folders")
    group.add_argument(
        "--threshold",
        type=float,
        default=None,
        help=f"Pause in seconds that starts a new product (default {config.PRODUCT_GAP_SECONDS:g}; higher keeps more angles together)",
    )
    group.add_argument("--dry-run", action="store_true", help="Print clusters without writing output/")
    group.set_defaults(func=group_cmd.run)

    describe = sub.add_parser("describe", help="Draft title, description, category, and condition")
    describe.add_argument("--limit", type=int, default=None, help="Only describe this many uncached products")
    describe.add_argument("--force", action="store_true", help="Ignore the description cache")
    describe.add_argument("--yes", "-y", action="store_true", help="Skip the cost confirmation prompt")
    describe.set_defaults(func=describe_cmd.run)

    export = sub.add_parser("export", help="Write the manifest, CSV, and listing.txt in each product folder")
    export.add_argument(
        "--into",
        type=Path,
        default=None,
        dest="publish_into",
        help="Also copy named product folders into this directory",
    )
    export.set_defaults(func=export_cmd.run)

    pipeline = sub.add_parser("run", help="Ingest a photo folder and write product subfolders with listings")
    pipeline.add_argument("dump_folder", type=Path, help="Folder of mixed product photos (read-only)")
    pipeline.add_argument("--force", action="store_true", help="Rebuild working/ before ingesting")
    pipeline.add_argument(
        "--threshold",
        type=float,
        default=None,
        help=f"Pause in seconds that starts a new product (default {config.PRODUCT_GAP_SECONDS:g})",
    )
    pipeline.add_argument("--yes", "-y", action="store_true", help="Skip the vision cost prompt")
    pipeline.add_argument("--skip-describe", action="store_true", help="Do not call the vision model")
    pipeline.set_defaults(func=run_cmd.run)

    ui = sub.add_parser("ui", help="Open a page where you can drop a photo folder")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-open", action="store_true", help="Do not open a browser window")
    ui.set_defaults(func=ui_cmd.run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        setup_logging(args.verbose)
        return int(args.func(args))
    except KeyboardInterrupt:
        LOG.error("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
