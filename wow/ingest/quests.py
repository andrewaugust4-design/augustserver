"""Quest Browser data: which quests exist in Forever, which are new, and
whatever details are readable for each.

What the client actually has (checked 2026-09-23, Forever 1.60.1.69977 vs
Era 1.15.9.69722): the quest table, QuestV2, is only ID + UniqueBitFlag in
*both* builds. Names, zones, levels, races and text are server-side in
Classic, sent to the client as players see quests. So:

- **Which quests exist / "New to Forever"**: Forever QuestV2 IDs absent from
  Era's → 1,795, exactly the trackers' count. Three of those are vanilla quests
  that were only added to the client list (A Crew Under Fire, Master Angler,
  Junkboxes Needed); they're not new, so 1,792 get the badge.
- **Carryover details** (title, zone, levels, races → faction, chain,
  description/objectives): the vanilla 1.12 reference in
  reference/vanilla_quests.json.gz (cmangos classic-db). Labelled as
  reference data, because Forever may have changed a carryover quest
  server-side and we can't see it.
- **New quests**: no public source has their names/zones/levels (Wowhead has
  them, but its robots.txt disallows automated fetching, so each quest links
  out instead). What the client does carry for a few: a map location
  (QuestPOIBlob → UiMap, 22 quests) and quest-line names (QuestLine).
- **Rewards**: server-side and retunable, so never shown ("pending").
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from .wago_client import read_csv

REFERENCE = Path(__file__).resolve().parent.parent / "reference" / "vanilla_quests.json.gz"

FOREVER_TABLES = ("QuestV2", "QuestSort", "AreaTable", "QuestPOIBlob", "UiMap", "QuestLine", "QuestLineXQuest")
ERA_TABLES = ("QuestV2", "AreaTable")

# Vanilla race bits (1 << raceId-1): Alliance = Human, Dwarf, Night Elf, Gnome.
ALLIANCE_RACES = 1 | 4 | 8 | 64
HORDE_RACES = 2 | 16 | 32 | 128

# Validation anchors (reference data): id -> (zone, faction, min level)
ANCHORS = {
    7: ("Elwynn Forest", "alliance", 1),        # Kobold Camp Cleanup
    4641: ("Durotar", "horde", 1),              # Your Place In The World
    2: ("Ashenvale", "horde", 20),              # Sharptalon's Claw
    783: ("Elwynn Forest", "alliance", 1),      # A Threat Within
}


def faction_of(races: int) -> str:
    a, h = bool(races & ALLIANCE_RACES), bool(races & HORDE_RACES)
    if races == 0 or (a and h) or not (a or h):
        return "both"
    return "alliance" if a else "horde"


def _reference() -> dict[int, dict]:
    with gzip.open(REFERENCE, "rt", encoding="utf-8") as f:
        doc = json.load(f)
    cols = doc["columns"]
    return {row[0]: dict(zip(cols, row)) for row in doc["rows"]}


def _areas(*build_dirs: Path) -> dict[int, tuple[str, int]]:
    """AreaTable id -> (name, parent id); later builds win."""
    out: dict[int, tuple[str, int]] = {}
    for d in build_dirs:
        for r in read_csv(d / "AreaTable.csv"):
            out[int(r["ID"])] = (r["AreaName_lang"], int(r["ParentAreaID"] or 0))
    return out


def build_quests(forever_dir: Path, era_dir: Path) -> tuple[list[tuple], dict]:
    ref = _reference()
    forever_ids = {int(r["ID"]) for r in read_csv(forever_dir / "QuestV2.csv")}
    era_ids = {int(r["ID"]) for r in read_csv(era_dir / "QuestV2.csv")}
    areas = _areas(era_dir, forever_dir)
    sorts = {int(r["ID"]): r["SortName_lang"] for r in read_csv(forever_dir / "QuestSort.csv")}
    ui_maps = {int(r["ID"]): r["Name_lang"] for r in read_csv(forever_dir / "UiMap.csv")}
    poi_zone: dict[int, str] = {}
    for r in read_csv(forever_dir / "QuestPOIBlob.csv"):
        name = ui_maps.get(int(r["UiMapID"]))
        if name:
            poi_zone.setdefault(int(r["QuestID"]), name)
    line_names = {int(r["ID"]): r["Name_lang"] for r in read_csv(forever_dir / "QuestLine.csv")}
    quest_line: dict[int, tuple[str, int]] = {}
    for r in read_csv(forever_dir / "QuestLineXQuest.csv"):
        if int(r["QuestLineID"]) in line_names:
            quest_line[int(r["QuestID"])] = (line_names[int(r["QuestLineID"])], int(r["OrderIndex"]))

    def zone_for(zone_or_sort: int) -> tuple[str | None, str | None]:
        """(zone, subzone) — subzones roll up to their parent zone for filtering."""
        if zone_or_sort > 0:
            name, parent = areas.get(zone_or_sort, (None, 0))
            if parent and parent in areas:
                return areas[parent][0], name
            return name, None
        if zone_or_sort < 0:
            return sorts.get(-zone_or_sort), None  # a category, e.g. "Warrior", "Seasonal"
        return None, None

    rows, stats = [], {"new": 0, "classic_detailed": 0, "classic_unrevealed": 0, "reference_not_in_client": 0,
                       "vanilla_added_to_client": 0, "removed_from_forever": len(era_ids - forever_ids)}
    for qid in sorted(forever_ids | set(ref)):
        r = ref.get(qid)
        in_client = qid in forever_ids
        if r is None and qid not in era_ids:
            status, revealed = "new", False
            stats["new"] += 1
        else:
            status, revealed = "classic", r is not None
            if r is None:
                stats["classic_unrevealed"] += 1
            else:
                stats["classic_detailed"] += 1
                stats["reference_not_in_client"] += not in_client
                stats["vanilla_added_to_client"] += in_client and qid not in era_ids
        if r:
            zone, subzone = zone_for(r["zone_or_sort"])
            detail = {
                "details": r["details"], "objectives": r["objectives"], "objective_texts": r["objective_texts"],
                "prev_quest": r["prev_quest"], "next_quest": r["next_quest"], "next_in_chain": r["next_in_chain"],
                "exclusive_group": r["exclusive_group"], "type": r["type"],
                "required_classes": r["required_classes"],
            }
            name, min_level = r["title"], r["min_level"] or None
            quest_level = r["quest_level"] if r["quest_level"] and r["quest_level"] > 0 else None
            races = r["required_races"] or 0
            faction = faction_of(races)
        else:
            zone, subzone = poi_zone.get(qid), None
            detail, name, min_level, quest_level, races, faction = {}, None, None, None, None, None
        if qid in quest_line:
            detail["quest_line"], detail["quest_line_step"] = quest_line[qid][0], quest_line[qid][1] + 1
        rows.append((qid, name, status, int(revealed), int(in_client), zone, subzone, min_level, quest_level,
                     faction, races, "reference" if r else "client", json.dumps(detail) if detail else None))
    stats["new_with_zone"] = sum(1 for q in poi_zone if q in forever_ids and q not in era_ids and q not in ref)
    return rows, stats


def check_anchors(rows: list[tuple]) -> list[str]:
    by_id = {r[0]: r for r in rows}
    problems = []
    for qid, (zone, faction, min_level) in ANCHORS.items():
        r = by_id.get(qid)
        got = (r[5], r[9], r[7]) if r else None
        if got != (zone, faction, min_level):
            problems.append(f"quest {qid}: expected {(zone, faction, min_level)}, got {got}")
    return problems


SCHEMA = """
DROP TABLE IF EXISTS quests;
CREATE TABLE quests (
    id INTEGER PRIMARY KEY,
    name TEXT,
    status TEXT NOT NULL,
    revealed INTEGER NOT NULL,
    in_client INTEGER NOT NULL,
    zone TEXT,
    subzone TEXT,
    min_level INTEGER,
    quest_level INTEGER,
    faction TEXT,
    races_mask INTEGER,
    source TEXT NOT NULL,
    detail_json TEXT
);
CREATE INDEX idx_quests_zone ON quests(zone);
CREATE INDEX idx_quests_status ON quests(status);
"""


def write_tables(conn, rows: list[tuple]) -> None:
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.executemany("INSERT INTO quests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
