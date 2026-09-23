"""CLI entrypoint: refresh /opt/wow/data/wow.db from the latest wago.tools
Forever beta + Classic Era builds.

    python -m ingest.run [--force] [--warm-icons]

Run once by hand after first deploy to populate the DB, then scheduled daily
by wow-refresh.timer (see deploy/). Idempotent: re-running on an unchanged
build just re-reads cached CSVs and rewrites the same rows.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from common import icons
from common.armor import ARMOR_TABLES
from common.constants import CLASSIC_ERA_PRODUCT, FOREVER_PRODUCT

from . import effects, quests, validate_character, wago_client
from .character_data import CHARACTER_TABLES, load_character_data
from .normalize import diff_items, load_armor_tables, load_budgets, parse_build, write_db
from .validate import ValidationError, validate

log = logging.getLogger("wow.ingest")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "wow.db"

TABLES = ("Item", "ItemSparse")
THUNDERFURY = 19019  # effect anchor: its Chance on hit line must render from the spell tables
FOREVER_ONLY_TABLES = ("RandPropPoints", *ARMOR_TABLES, *CHARACTER_TABLES)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download CSVs even if cached")
    parser.add_argument("--warm-icons", action="store_true",
                        help="after ingest, pre-fetch item icons that aren't cached yet (needs Blizzard API keys)")
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
    for table in FOREVER_ONLY_TABLES:
        wago_client.download_csv(table, forever_version, CACHE_DIR, force=args.force)
    for table in effects.ERA_TABLES:
        wago_client.download_csv(table, era_version, CACHE_DIR, force=args.force)
    for table in effects.FOREVER_TABLES:
        wago_client.download_csv(table, forever_version, CACHE_DIR, force=args.force)
    for table in quests.FOREVER_TABLES:
        wago_client.download_csv(table, forever_version, CACHE_DIR, force=args.force)
    for table in quests.ERA_TABLES:
        wago_client.download_csv(table, era_version, CACHE_DIR, force=args.force)

    budgets = load_budgets(CACHE_DIR / forever_version / "RandPropPoints.csv")
    forever_items = parse_build(
        CACHE_DIR / forever_version / "Item.csv",
        CACHE_DIR / forever_version / "ItemSparse.csv",
        budgets=budgets,
        armor_tables=load_armor_tables(CACHE_DIR / forever_version),
    )
    era_items = parse_build(
        CACHE_DIR / era_version / "Item.csv",
        CACHE_DIR / era_version / "ItemSparse.csv",
    )
    merged = diff_items(forever_items, era_items)
    counts = Counter(item["change_status"] for item in merged.values())

    try:
        validation_meta = validate(merged)
    except ValidationError as exc:
        log.error("Stat validation failed, keeping the previous DB: %s", exc)
        sys.exit(1)

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
        **validation_meta,
    }
    item_effects, effect_stats = effects.build_item_effects(merged, CACHE_DIR / era_version, CACHE_DIR / forever_version)
    for item_id, found in item_effects.items():
        merged[item_id]["effects"] = found
    log.info("Item effects: %s", effect_stats)
    anchor = " ".join(e["text"] for e in item_effects.get(THUNDERFURY, []))
    if "300 Nature damage" in anchor:
        log.info("Effect anchor: Thunderfury renders %r", anchor[:90] + "…")
    else:
        log.error("Effect anchor FAILED: Thunderfury's proc text is %r — check ingest/effects.py", anchor)
    set_rows, set_stats = effects.build_sets(merged, CACHE_DIR / era_version, CACHE_DIR / forever_version)
    log.info("Item sets: %s", set_stats)

    qdb = quests.fetch_questiedb(CACHE_DIR)
    quest_rows, quest_stats = quests.build_quests(CACHE_DIR / forever_version, CACHE_DIR / era_version, qdb)
    log.info("Quests: %s", quest_stats)
    for problem in quests.check_anchors(quest_rows):
        log.error("Quest anchor FAILED: %s", problem)
    xp = quests.xp_check(CACHE_DIR / forever_version, qdb)
    if xp["xp_mismatches"]:
        log.error("Quest XP check: %d/%d QuestieDB values aren't a QuestXP tier cell, e.g. %s",
                  xp["xp_checked"] - xp["xp_matched"], xp["xp_checked"], xp["xp_mismatches"])
    else:
        log.info("Quest XP check: %d/%d QuestieDB values match the QuestXP table", xp["xp_matched"], xp["xp_checked"])
    quest_stats.update(xp_checked=xp["xp_checked"], xp_matched=xp["xp_matched"])
    meta.update({f"quests_{k}": str(v) for k, v in quest_stats.items()})

    character = load_character_data(CACHE_DIR / forever_version)
    write_db(DB_PATH, meta, merged, budgets, character, set_rows, quest_rows)
    log.info("Ingest complete: %s", meta)

    # Set-builder sheet cross-checks: logged, never blocking (see validate_character.py).
    conn = sqlite3.connect(DB_PATH)
    try:
        char_summary = validate_character.run(conn, emit=log.info)
        with conn:
            conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", char_summary.items())
    except Exception:
        log.exception("Character sheet validation crashed (the refresh itself succeeded)")
    finally:
        conn.close()

    if args.warm_icons:
        if not icons.enabled():
            log.warning("--warm-icons: Blizzard credentials not set, skipping.")
        else:
            # Highest ilvl first — those are the items people browse most.
            item_ids = [it["id"] for it in sorted(merged.values(), key=lambda it: -it["item_level"])]
            log.info("Warming icons for %d items into %s", len(item_ids), icons.ICON_DIR)
            log.info("Icon warm done: %s", icons.warm(item_ids))


if __name__ == "__main__":
    main()
