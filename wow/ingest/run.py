"""CLI entrypoint: refresh /opt/wow/data/wow.db from the latest wago.tools
Forever beta + Classic Era builds.

    python -m ingest.run [--force]

Run once by hand after first deploy to populate the DB, then scheduled daily
by wow-refresh.timer (see deploy/). Idempotent: re-running on an unchanged
build just re-reads cached CSVs and rewrites the same rows.
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from common.constants import CLASSIC_ERA_PRODUCT, FOREVER_PRODUCT

from . import wago_client
from .normalize import diff_items, parse_build, write_db

log = logging.getLogger("wow.ingest")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "wow.db"

TABLES = ("Item", "ItemSparse")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download CSVs even if cached")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    builds = wago_client.get_builds()
    forever_build = wago_client.latest_build(FOREVER_PRODUCT, builds)
    era_build = wago_client.latest_build(CLASSIC_ERA_PRODUCT, builds)
    forever_version = forever_build["version"]
    era_version = era_build["version"]

    log.info("Forever candidate: product=%s version=%s created_at=%s", FOREVER_PRODUCT, forever_version, forever_build.get("created_at"))
    log.info("Classic Era baseline: product=%s version=%s created_at=%s", CLASSIC_ERA_PRODUCT, era_version, era_build.get("created_at"))

    description = wago_client.describe_build(forever_build["build_config"])
    if description is None:
        log.warning("Could not confirm build_config description for %s — proceeding on product name alone. "
                    "If item data looks wrong, re-check FOREVER_PRODUCT in common/constants.py against "
                    "https://wago.tools/builds", forever_version)
    elif "forever" not in description.lower():
        log.warning("Latest %s build_config description is %r — does not mention 'Forever'. "
                    "The product may have reverted to a different beta cycle; verify before trusting this data.",
                    FOREVER_PRODUCT, description)
    else:
        log.info("Confirmed via build_config description: %s", description)

    for version in (forever_version, era_version):
        for table in TABLES:
            wago_client.download_csv(table, version, CACHE_DIR, force=args.force)

    forever_items = parse_build(
        CACHE_DIR / forever_version / "Item.csv",
        CACHE_DIR / forever_version / "ItemSparse.csv",
        stats_absolute=False,
    )
    era_items = parse_build(
        CACHE_DIR / era_version / "Item.csv",
        CACHE_DIR / era_version / "ItemSparse.csv",
        stats_absolute=True,
    )
    merged = diff_items(forever_items, era_items)
    counts = Counter(item["change_status"] for item in merged.values())

    meta = {
        "forever_product": FOREVER_PRODUCT,
        "forever_build": forever_version,
        "forever_build_id": forever_version.rsplit(".", 1)[-1],
        "classic_era_build": era_version,
        "classic_era_build_id": era_version.rsplit(".", 1)[-1],
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "item_count": str(len(merged)),
        "new_count": str(counts.get("new", 0)),
        "changed_count": str(counts.get("changed", 0)),
        "unchanged_count": str(counts.get("unchanged", 0)),
    }
    write_db(DB_PATH, meta, merged)
    log.info("Ingest complete: %s", meta)


if __name__ == "__main__":
    main()
