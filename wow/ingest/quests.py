"""Quest Browser data: which quests exist in Forever, which are new, and
everything readable about each, including rewards.

Sources (checked 2026-09-23, Forever 1.60.1.69977 vs Era 1.15.9.69722):

- **Forever side: wago.tools client DB2.** The client's quest table, QuestV2,
  is only ID + UniqueBitFlag in *both* builds; quest names, levels and text
  are server-side in Classic. So the client tells us which quests exist:
  Forever ids absent from Era's and from QuestieDB's Classic set are **New to
  Forever** (1,792; the trackers' 1,795 includes three vanilla quests that
  were only newly listed). For a few new quests the client adds a map
  location (QuestPOIBlob → UiMap, 22) or a quest-line name (QuestLine).
- **Carryover detail + rewards: QuestieDB** (ingest/questiedb.py) — name,
  zone, levels, races → faction, classes, objectives, quest giver / turn-in,
  chain, reward items, XP, reputation.
- **XP** is QuestieDB's full at-level reward. The client's QuestXP DB2
  (level × difficulty tier; identical in Forever and Era) can't compute it on
  its own because the tier is server-side, so it's the cross-check instead:
  every QuestieDB value must be one of its level's tier cells (3,490/3,490 on
  2026-09-23). Quests QuestieDB gives no XP for show none rather than a guess.
- **What QuestieDB lacks**, from the cmangos 1.12 reference
  (reference/vanilla_quests.json.gz): the long description, which reward
  items are choose-one vs guaranteed (+ counts), and money.
- **Wowhead** is only a link the visitor clicks. Its robots.txt disallows
  automated agents, and nothing here fetches it.
- **New quests' rewards and details** are server-side until seen in-world:
  "not yet revealed", filled in by a later QuestieDB/client refresh.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from . import questiedb
from .questiedb import values
from .wago_client import read_csv

REFERENCE = Path(__file__).resolve().parent.parent / "reference" / "vanilla_quests.json.gz"

FOREVER_TABLES = ("QuestV2", "QuestSort", "AreaTable", "QuestPOIBlob", "UiMap", "QuestLine", "QuestLineXQuest",
                  "Faction", "QuestXP")
ERA_TABLES = ("QuestV2", "AreaTable")

# Vanilla race bits (1 << raceId-1): Alliance = Human, Dwarf, Night Elf, Gnome.
ALLIANCE_RACES = 1 | 4 | 8 | 64
HORDE_RACES = 2 | 16 | 32 | 128

# Validation anchors: id -> (zone, faction, min level, a reward that must be there)
ANCHORS = {
    7: ("Elwynn Forest", "alliance", 1, {"xp": 170}),              # Kobold Camp Cleanup
    4641: ("Durotar", "horde", 1, None),                           # Your Place In The World
    2: ("Ashenvale", "horde", 20, {"xp": 2450}),                   # Sharptalon's Claw (no item reward)
    176: ("Elwynn Forest", "alliance", 5, {"item": 6084}),         # Wanted: "Hogger" → Stormwind Guard Leggings (choice)
    783: ("Elwynn Forest", "alliance", 1, None),                   # A Threat Within
}


def faction_of(races: int | None) -> str:
    races = races or 0
    a, h = bool(races & ALLIANCE_RACES), bool(races & HORDE_RACES)
    if races == 0 or (a and h) or not (a or h):
        return "both"
    return "alliance" if a else "horde"


def _cmangos() -> dict[int, dict]:
    with gzip.open(REFERENCE, "rt", encoding="utf-8") as f:
        doc = json.load(f)
    return {row[0]: dict(zip(doc["columns"], row)) for row in doc["rows"]}


def _areas(*build_dirs: Path) -> dict[int, tuple[str, int]]:
    out: dict[int, tuple[str, int]] = {}
    for d in build_dirs:
        for r in read_csv(d / "AreaTable.csv"):
            out[int(r["ID"])] = (r["AreaName_lang"], int(r["ParentAreaID"] or 0))
    return out


def xp_check(forever_dir: Path, qdb: dict) -> dict:
    """How many QuestieDB XP values are a real QuestXP[level][tier] cell."""
    tiers = {int(r["ID"]): {int(v) for k, v in r.items() if k.startswith("Difficulty_")}
             for r in read_csv(forever_dir / "QuestXP.csv")}
    checked = [(qid, lvl, qdb["xp"][qid]) for qid, lvl in qdb["xp_level"].items()
               if lvl in tiers and qdb["xp"].get(qid)]
    bad = [(qid, lvl, xp) for qid, lvl, xp in checked if xp not in tiers[lvl]]
    return {"xp_checked": len(checked), "xp_matched": len(checked) - len(bad), "xp_mismatches": bad[:10]}


def build_quests(forever_dir: Path, era_dir: Path, qdb: dict) -> tuple[list[tuple], dict]:
    cm = _cmangos()
    quests_qdb = qdb["quests"]
    forever_ids = {int(r["ID"]) for r in read_csv(forever_dir / "QuestV2.csv")}
    era_ids = {int(r["ID"]) for r in read_csv(era_dir / "QuestV2.csv")}
    areas = _areas(era_dir, forever_dir)
    sorts = {int(r["ID"]): r["SortName_lang"] for r in read_csv(forever_dir / "QuestSort.csv")}
    factions = {int(r["ID"]): r["Name_lang"] for r in read_csv(forever_dir / "Faction.csv")}
    ui_maps = {int(r["ID"]): r["Name_lang"] for r in read_csv(forever_dir / "UiMap.csv")}
    poi_zone: dict[int, str] = {}
    for r in read_csv(forever_dir / "QuestPOIBlob.csv"):
        if ui_maps.get(int(r["UiMapID"])):
            poi_zone.setdefault(int(r["QuestID"]), ui_maps[int(r["UiMapID"])])
    line_names = {int(r["ID"]): r["Name_lang"] for r in read_csv(forever_dir / "QuestLine.csv")}
    quest_line = {int(r["QuestID"]): (line_names[int(r["QuestLineID"])], int(r["OrderIndex"]) + 1)
                  for r in read_csv(forever_dir / "QuestLineXQuest.csv") if int(r["QuestLineID"]) in line_names}
    # Reward item names/quality from the Forever client where it has them (it covers non-gear too).
    item_info = {int(r["ID"]): (r.get("Display_lang") or "", int(r.get("OverallQualityID") or 1))
                 for r in read_csv(forever_dir / "ItemSparse.csv")}

    def area_name(area_id) -> str | None:
        return areas.get(area_id, (None, 0))[0] if area_id else None

    def zone_for(zone_or_sort) -> tuple[str | None, str | None]:
        if zone_or_sort and zone_or_sort > 0:
            name, parent = areas.get(zone_or_sort, (None, 0))
            if parent and parent in areas:
                return areas[parent][0], name
            return name, None
        if zone_or_sort and zone_or_sort < 0:
            return sorts.get(-zone_or_sort), None  # a category: class quests, professions, events
        return None, None

    def item_ref(iid: int) -> dict:
        name, quality = item_info.get(iid, ("", 1))
        return {"id": iid, "name": name or qdb["item_names"].get(iid) or f"Item {iid}", "quality": quality}

    def npc_ref(nid: int) -> dict:
        name, zone = qdb["npcs"].get(nid, (None, None))
        return {"id": nid, "name": name or f"NPC {nid}", "zone": area_name(zone)}

    def ends(tbl) -> dict:
        """startedBy/finishedBy → {npcs, objects, items} with names."""
        tbl = tbl or {}
        out = {
            "npcs": [npc_ref(n) for n in values(tbl.get(1))],
            "objects": [{"id": o, "name": qdb["objects"].get(o) or f"Object {o}"} for o in values(tbl.get(2))],
            "items": [item_ref(i) for i in values(tbl.get(3))],
        }
        return {k: v for k, v in out.items() if v}

    def objectives(obj) -> list[str]:
        obj = obj or {}
        out = []
        for entry in values(obj.get(1)):  # creatures
            nid = values(entry)[0] if values(entry) else None
            text = entry.get(2) if isinstance(entry, dict) else None
            out.append(f"Slay: {text or qdb['npcs'].get(nid, (f'NPC {nid}',))[0]}")
        for entry in values(obj.get(2)):  # objects
            oid = values(entry)[0] if values(entry) else None
            out.append(f"Use: {(entry.get(2) if isinstance(entry, dict) else None) or qdb['objects'].get(oid) or f'Object {oid}'}")
        for entry in values(obj.get(3)):  # items
            iid = values(entry)[0] if values(entry) else None
            out.append(f"Collect: {(entry.get(2) if isinstance(entry, dict) else None) or item_ref(iid)['name']}")
        rep = obj.get(4)
        if rep:
            v = values(rep)
            out.append(f"Reputation: {factions.get(v[0], f'Faction {v[0]}')} — {v[1] if len(v) > 1 else '?'}")
        return out

    def rewards(qid: int, q: dict) -> dict:
        ref = cm.get(qid, {})
        choice = {i: c for i, c in ref.get("reward_choice", [])}
        guaranteed = {i: c for i, c in ref.get("reward_items", [])}
        items = []
        for iid in sorted(qdb["reward_items"].get(qid, [])):
            kind = "choice" if iid in choice else "guaranteed" if iid in guaranteed else "reward"
            items.append({**item_ref(iid), "count": choice.get(iid) or guaranteed.get(iid) or 1, "kind": kind})
        money = ref.get("money") or 0
        rep = [{"faction": factions.get(values(r)[0], f"Faction {values(r)[0]}"), "value": values(r)[1]}
               for r in values(q.get("reputationReward")) if len(values(r)) >= 2]
        return {"items": items, "money": money if money > 0 else 0, "xp": qdb["xp"].get(qid), "reputation": rep}

    rows = []
    stats = {"new": 0, "classic_detailed": 0, "classic_unrevealed": 0, "detailed_not_in_client": 0,
             "vanilla_added_to_client": 0, "removed_from_forever": len(era_ids - forever_ids),
             "with_reward_items": 0}
    for qid in sorted(forever_ids | set(quests_qdb)):
        q = quests_qdb.get(qid)
        in_client = qid in forever_ids
        if q is None and qid not in era_ids and qid not in cm:
            status, revealed = "new", False
            stats["new"] += 1
        elif q is None:
            status, revealed = "classic", False
            stats["classic_unrevealed"] += 1
        else:
            status, revealed = "classic", True
            stats["classic_detailed"] += 1
            stats["detailed_not_in_client"] += not in_client
            stats["vanilla_added_to_client"] += in_client and qid not in era_ids

        if q:
            zone, subzone = zone_for(q.get("zoneOrSort"))
            chain = {
                "pre_single": values(q.get("preQuestSingle")), "pre_group": values(q.get("preQuestGroup")),
                "next_in_chain": q.get("nextQuestInChain"), "exclusive_to": values(q.get("exclusiveTo")),
                "breadcrumbs": values(q.get("breadcrumbs")), "breadcrumb_for": q.get("breadcrumbForQuestId"),
                "parent": q.get("parentQuest"),
            }
            reward = rewards(qid, q)
            stats["with_reward_items"] += bool(reward["items"])
            detail = {
                "objectives_text": [t for t in values(q.get("objectivesText")) if t],
                "objectives": objectives(q.get("objectives")),
                "details": cm.get(qid, {}).get("details") or None,
                "started_by": ends(q.get("startedBy")), "finished_by": ends(q.get("finishedBy")),
                "chain": {k: v for k, v in chain.items() if v},
                "required_classes": q.get("requiredClasses") or 0,
                "rewards": reward,
            }
            name = q.get("name")
            min_level = q.get("requiredLevel") or None
            quest_level = q.get("questLevel") if (q.get("questLevel") or 0) > 0 else None
            races = q.get("requiredRaces") or 0
            faction = faction_of(races)
            xp = reward["xp"] or None
            objective_count = len(detail["objectives"])
        else:
            zone, subzone = poi_zone.get(qid), None
            detail, name, min_level, quest_level, races, faction = {}, None, None, None, None, None
            xp = objective_count = None
        if qid in quest_line:
            detail["quest_line"], detail["quest_line_step"] = quest_line[qid]
        rows.append((qid, name, status, int(revealed), int(in_client), zone, subzone, min_level, quest_level,
                     faction, races, "questiedb" if q else "client", json.dumps(detail) if detail else None, xp,
                     objective_count))
    stats["new_with_zone"] = sum(1 for r in rows if r[2] == "new" and r[5])
    stats["questiedb_version"] = qdb["version"]
    return rows, stats


def check_anchors(rows: list[tuple]) -> list[str]:
    by_id = {r[0]: r for r in rows}
    problems = []
    for qid, (zone, faction, min_level, reward) in ANCHORS.items():
        r = by_id.get(qid)
        got = (r[5], r[9], r[7]) if r else None
        if got != (zone, faction, min_level):
            problems.append(f"quest {qid}: expected {(zone, faction, min_level)}, got {got}")
            continue
        rw = json.loads(r[12] or "{}").get("rewards", {})
        if reward and "xp" in reward and rw.get("xp") != reward["xp"]:
            problems.append(f"quest {qid}: expected {reward['xp']} XP, got {rw.get('xp')}")
        if reward and "item" in reward and reward["item"] not in [i["id"] for i in rw.get("items", [])]:
            problems.append(f"quest {qid}: expected reward item {reward['item']}, got {rw.get('items')}")
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
    detail_json TEXT,
    xp INTEGER,
    objective_count INTEGER
);
CREATE INDEX idx_quests_zone ON quests(zone);
CREATE INDEX idx_quests_status ON quests(status);
"""


def write_tables(conn, rows: list[tuple]) -> None:
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.executemany("INSERT INTO quests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def fetch_questiedb(cache_dir: Path) -> dict:
    return questiedb.load(questiedb.fetch(cache_dir / "questiedb"))
