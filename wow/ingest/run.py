"""CLI entrypoint: refresh /opt/wow/data/wow.db from the latest Forever beta
+ Classic Era builds.

    python -m ingest.run [--force] [--warm-icons] [--source auto|wago|cdn]

DB2 tables come from wago.tools' CSV export (primary). If Blizzard's CDN
already serves a newer build than wago.tools has processed, that build's
tables are extracted straight from the CDN instead (ingest/cdn_client.py) —
same CSV shape, same cache dir, so nothing downstream changes. ``--source
cdn`` (or ``--cdn``) forces the CDN path for the Forever build. This is a
parity fallback for wago.tools lag, not a way past Blizzard's withheld-key
encryption: still-encrypted rows are skipped either way.

Run once by hand after first deploy to populate the DB, then scheduled daily
by wow-refresh.timer (see deploy/). Idempotent: re-running on an unchanged
build just re-reads cached CSVs and rewrites the same rows.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from common import icons
from common.armor import ARMOR_TABLES
from common.constants import CLASSIC_ERA_PRODUCT, FOREVER_PRODUCT

from . import cdn_client, downrank, effects, quests, talents, validate_character, wago_client
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

# Every DB2 table the ingest reads, per build.
FOREVER_TABLES = tuple(dict.fromkeys((*TABLES, *FOREVER_ONLY_TABLES, *effects.FOREVER_TABLES, *quests.FOREVER_TABLES,
                                      *talents.FOREVER_TABLES, *downrank.FOREVER_TABLES)))
ERA_TABLES = tuple(dict.fromkeys((*TABLES, *effects.ERA_TABLES, *quests.ERA_TABLES, *talents.ERA_TABLES,
                                  *downrank.ERA_TABLES)))


def _build_id(version: str) -> int:
    return int(version.rsplit(".", 1)[-1])


def resolve_build(product: str, wago_builds: dict | None, source: str) -> dict:
    """Pick the build (and where its tables come from) for one product.

    wago.tools is primary. ``auto`` switches to Blizzard's CDN only when the
    CDN already serves a newer build than wago.tools lists (wago.tools
    lagging) or wago.tools is unreachable; ``cdn`` forces the CDN build.
    Returns ``{source, version, build_config, cdn_config, ..., icon_build,
    cdn_build}`` — ``cdn_build`` is kept for a mid-download wago fallback."""
    wago = wago_client.latest_build(product, wago_builds) if wago_builds and wago_builds.get(product) else None
    icon_build = wago["version"] if wago else None
    if source == "wago" and wago is None:
        raise RuntimeError(f"wago.tools returned no builds for product {product!r}")

    cdn = None
    if source != "wago":
        try:
            cdn = cdn_client.current_build(product)
            log.info("%s on Blizzard's CDN: %s (build_config=%s cdn_config=%s)",
                     product, cdn["version"], cdn["build_config"], cdn["cdn_config"])
        except Exception:
            if source == "cdn" or wago is None:
                raise
            log.warning("Blizzard version service lookup failed for %s — staying on wago.tools", product, exc_info=True)

    if source == "cdn" or wago is None or (cdn and _build_id(cdn["version"]) > _build_id(wago["version"])):
        if source == "auto" and wago is not None:
            log.warning("wago.tools is behind for %s (has %s, CDN serves %s) — extracting from the CDN",
                        product, wago["version"], cdn["version"])
        return {**cdn, "source": "cdn", "icon_build": icon_build, "cdn_build": cdn}
    return {**wago, "source": "wago", "icon_build": icon_build, "cdn_build": cdn}


def fetch_tables(build: dict, tables: tuple[str, ...], args: argparse.Namespace) -> None:
    """Get every table's CSV into CACHE_DIR/<version>/ from the chosen source.
    In auto mode a failed wago.tools download (e.g. the build is listed but
    not exported yet) falls back to the CDN when it serves the same build."""
    if build["source"] == "cdn":
        cdn_client.extract_tables(build, tables, CACHE_DIR, force=args.force)
        return
    try:
        for table in tables:
            wago_client.download_csv(table, build["version"], CACHE_DIR, force=args.force)
    except Exception:
        cdn = build.get("cdn_build")
        if args.source != "auto" or not cdn or cdn["version"] != build["version"]:
            raise
        log.warning("wago.tools download failed for %s — extracting it from the CDN instead", build["version"], exc_info=True)
        build.update({**cdn, "source": "cdn", "icon_build": build["icon_build"]})
        cdn_client.extract_tables(cdn, tables, CACHE_DIR, force=args.force)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download CSVs even if cached")
    parser.add_argument("--warm-icons", action="store_true",
                        help="after ingest, pre-fetch item icons that aren't cached yet (needs Blizzard API keys)")
    parser.add_argument("--source", choices=("auto", "wago", "cdn"), default="auto",
                        help="where the Forever build's DB2 tables come from: auto (wago.tools, or Blizzard's CDN "
                             "when it has a newer build than wago.tools), wago, or cdn (force a CDN pull now)")
    parser.add_argument("--cdn", dest="source", action="store_const", const="cdn", help="same as --source cdn")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        builds = wago_client.get_builds()
    except Exception:
        if args.source == "wago":
            raise
        log.warning("wago.tools build list unavailable — resolving builds from Blizzard's CDN", exc_info=True)
        builds = None

    forever = resolve_build(FOREVER_PRODUCT, builds, args.source)
    era = resolve_build(CLASSIC_ERA_PRODUCT, builds, "wago" if args.source == "wago" else "auto")
    forever_version = forever["version"]
    era_version = era["version"]

    log.info("Forever candidate: product=%s version=%s source=%s", FOREVER_PRODUCT, forever_version, forever["source"])
    log.info("Classic Era baseline: product=%s version=%s source=%s", CLASSIC_ERA_PRODUCT, era_version, era["source"])

    fetch_tables(forever, FOREVER_TABLES, args)
    fetch_tables(era, ERA_TABLES, args)

    if forever["source"] == "cdn":
        description = cdn_client.build_name(CACHE_DIR, forever_version)
    else:
        description = wago_client.describe_build(forever["build_config"])
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
        "forever_source": forever["source"],
        "forever_build_config": forever.get("build_config") or "",
        "forever_cdn_config": forever.get("cdn_config") or "",
        # wago.tools serves talent icons by FileDataID for builds it knows; a
        # CDN-only build isn't one of those yet, so point icon fetches at the
        # newest Forever build wago.tools has.
        "icon_build": forever["icon_build"] or "",
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

    talent_rows, talent_stats, talent_problems = talents.build_talents(CACHE_DIR / forever_version, CACHE_DIR / era_version)
    log.info("Talents: %s", talent_stats)
    for problem in talent_problems:
        log.error("Talent check FAILED: %s", problem)
    meta.update({f"talents_{k}": str(v) for k, v in talent_stats.items()})
    meta["talents_problems"] = str(len(talent_problems))

    character = load_character_data(CACHE_DIR / forever_version)
    base_mana_60 = {row[0]: row[2] for row in character["class_level"] if row[1] == 60}
    downrank_rows, downrank_stats, downrank_problems = downrank.build_downrank(
        CACHE_DIR / forever_version, CACHE_DIR / era_version, base_mana_60)
    log.info("Downrank: %s", downrank_stats)
    for problem in downrank_problems:
        log.error("Downrank anchor FAILED: %s", problem)
    meta.update({f"downrank_{k}": str(v) for k, v in downrank_stats.items()})
    meta["downrank_problems"] = str(len(downrank_problems))

    write_db(DB_PATH, meta, merged, budgets, character, set_rows, quest_rows, talent_rows, downrank_rows)
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

    # Talent icons come from wago.tools by FileDataID (no API keys needed);
    # already-cached ones are skipped, so this is quick after the first run.
    fdids = sorted({t["icon"] for r in talent_rows for t in json.loads(r[4])["talents"] if t["icon"]}
                   | {tr["icon"] for r in talent_rows for tr in json.loads(r[4])["trees"] if tr["icon"]})
    if forever["icon_build"]:
        log.info("Talent icons: %s", icons.warm_file_icons(fdids, forever["icon_build"]))
    else:
        log.warning("Talent icons: wago.tools knows no Forever build yet, skipping the warm")

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
