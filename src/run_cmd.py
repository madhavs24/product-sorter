"""One command: picture folder in, product folders with listings out.

The original photos are only read. Results are written to output/ and, when
you pass the dump folder, to <dump>/sorted/ as well.
"""

from __future__ import annotations

from pathlib import Path

from src import describe_cmd, export_cmd, group_cmd, ingest_cmd
from src.utils import LOG


def run(args) -> int:
    dump = Path(args.dump_folder).expanduser()
    code = ingest_cmd.run(args)
    if code:
        return code

    args.dry_run = False
    code = group_cmd.run(args)
    if code:
        return code

    if args.skip_describe:
        LOG.info("Skipping describe.")
    else:
        args.limit = None
        described = describe_cmd.run(args)
        if described:
            LOG.error("Describe did not finish. Exporting whatever listings are already saved.")

    args.publish_into = dump / "sorted"
    export_code = export_cmd.run(args)
    if export_code:
        return export_code
    LOG.info("Product folders with listing.txt are in %s", args.publish_into)
    return 0
