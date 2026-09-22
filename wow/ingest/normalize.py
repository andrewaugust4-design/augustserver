"""Item.csv + ItemSparse.csv -> normalized item records, Forever-vs-Classic-Era
diff, and the SQLite write. See common/constants.py and common/proficiency.py
for the verified enum values this relies on.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from common.constants import (
    ARMOR_MATERIAL_NAMES,
    INVTYPE_TO_SLOT,
    ITEM_CLASS_ARMOR,
    ITEM_CLASS_WEAPON,
    WEAPON_SUBCLASS_NAMES,
)
from .wago_client import read_csv

# Standard ItemModType ids that actually show up on Classic-era gear. Anything
# not listed here just renders as "Stat <id>" rather than blocking ingest.
STAT_NAMES = {
    0: "Mana", 1: "Health", 3: "Agility", 4: "Strength", 5: "Intellect",
    6: "Spirit", 7: "Stamina", 12: "Defense Rating", 13: "Dodge Rating",
    14: "Parry Rating", 15: "Block Rating", 16: "Melee Hit Rating",
    17: "Ranged Hit Rating", 18: "Spell Hit Rating", 19: "Melee Crit Rating",
    20: "Ranged Crit Rating", 21: "Spell Crit Rating", 31: "Melee Haste Rating",
    32: "Ranged Haste Rating", 33: "Spell Haste Rating", 35: "Hit Rating",
    36: "Critical Strike Rating", 37: "Resilience Rating", 38: "Haste Rating",
    39: "Expertise Rating", 40: "Attack Power", 41: "Ranged Attack Power",
    42: "Feral Attack Power", 43: "Spell Healing Done", 44: "Spell Damage Done",
    45: "Mana Regeneration", 46: "Armor Penetration Rating", 47: "Spell Power",
    48: "Health Regen", 49: "Spell Penetration", 50: "Block Value",
}


def stat_name(stat_id: int) -> str:
    return STAT_NAMES.get(stat_id, f"Stat {stat_id}")


DIFF_FIELDS = ("name", "quality", "slot", "material", "required_level", "item_level")


def parse_build(item_csv: Path, itemsparse_csv: Path, *, stats_absolute: bool) -> dict[int, dict]:
    """Parse one build's Item + ItemSparse CSVs into {item_id: record}.

    `stats_absolute` selects which ItemSparse fields carry the stat values:
    True (Classic Era) reads StatModifier_bonusAmount_N (real numbers);
    False (Forever) reads StatPercentEditor_N, a datamined per-stat weight
    in basis points against an item-level budget we don't have — so those
    values are marked provisional rather than treated as final numbers.
    """
    items_by_id = {row["ID"]: row for row in read_csv(item_csv)}
    out: dict[int, dict] = {}

    for row in read_csv(itemsparse_csv):
        item_row = items_by_id.get(row["ID"])
        if item_row is None:
            continue
        try:
            item_class = int(item_row["ClassID"])
            item_subclass = int(item_row["SubclassID"])
            inventory_type = int(item_row["InventoryType"])
        except (KeyError, ValueError):
            continue
        if item_class not in (ITEM_CLASS_WEAPON, ITEM_CLASS_ARMOR):
            continue
        slot = INVTYPE_TO_SLOT.get(inventory_type)
        if slot is None:
            continue  # shirt/tabard/bag/ammo/etc — not a gear-browser slot

        name = (row.get("Display_lang") or "").strip()
        if not name:
            continue

        try:
            allowable_class_mask = int(row.get("AllowableClass") or -1)
        except ValueError:
            allowable_class_mask = -1

        stats = []
        for i in range(10):
            stat_raw = row.get(f"StatModifier_bonusStat_{i}")
            if stat_raw is None:
                break
            try:
                stat_id = int(stat_raw)
            except ValueError:
                continue
            if stat_id < 0:
                continue
            if stats_absolute:
                amount = int(row.get(f"StatModifier_bonusAmount_{i}") or 0)
                if amount == 0:
                    continue
                stats.append({"stat_id": stat_id, "name": stat_name(stat_id), "amount": amount, "provisional": False})
            else:
                weight = int(row.get(f"StatPercentEditor_{i}") or row.get(f"StatPercentageOfSocket_{i}") or 0)
                if weight == 0:
                    continue
                stats.append({"stat_id": stat_id, "name": stat_name(stat_id), "weight_bp": weight, "provisional": True})

        if item_class == ITEM_CLASS_WEAPON:
            material = WEAPON_SUBCLASS_NAMES.get(item_subclass)
        else:
            material = ARMOR_MATERIAL_NAMES.get(item_subclass)  # None for misc/shield/relic slots

        item_id = int(row["ID"])
        out[item_id] = {
            "id": item_id,
            "name": name,
            "quality": int(row.get("OverallQualityID") or 0),
            "item_class": item_class,
            "item_subclass": item_subclass,
            "inventory_type": inventory_type,
            "slot": slot,
            "material": material,
            "required_level": int(row.get("RequiredLevel") or 0),
            "allowable_class_mask": allowable_class_mask,
            "item_level": int(row.get("ItemLevel") or 0),
            "stats": stats,
            "provisional": not stats_absolute,
        }

    return out


def diff_items(forever_items: dict[int, dict], classic_items: dict[int, dict]) -> dict[int, dict]:
    """Attach change_status + a per-field diff to each Forever item, comparing
    against the Classic Era baseline. Items that only exist in Classic Era
    (cut from Forever) aren't included — the browser lists Forever's items."""
    merged: dict[int, dict] = {}
    for item_id, f_item in forever_items.items():
        c_item = classic_items.get(item_id)
        record = dict(f_item)
        if c_item is None:
            record["change_status"] = "new"
            record["diff"] = None
        else:
            diff = []
            for field in DIFF_FIELDS:
                if f_item[field] != c_item[field]:
                    diff.append({"field": field, "classic": c_item[field], "forever": f_item[field]})
            f_stat_ids = sorted(s["stat_id"] for s in f_item["stats"])
            c_stat_ids = sorted(s["stat_id"] for s in c_item["stats"])
            if f_stat_ids != c_stat_ids:
                diff.append({"field": "stats", "classic": c_item["stats"], "forever": f_item["stats"]})
            record["change_status"] = "changed" if diff else "unchanged"
            record["diff"] = diff or None
            record["classic_stats"] = c_item["stats"]
            record["classic_item_level"] = c_item["item_level"]
        merged[item_id] = record
    return merged


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    quality INTEGER NOT NULL,
    item_class INTEGER NOT NULL,
    item_subclass INTEGER NOT NULL,
    inventory_type INTEGER NOT NULL,
    slot TEXT NOT NULL,
    material TEXT,
    required_level INTEGER NOT NULL,
    allowable_class_mask INTEGER NOT NULL,
    item_level INTEGER NOT NULL,
    change_status TEXT NOT NULL,
    provisional INTEGER NOT NULL,
    stats_json TEXT NOT NULL,
    classic_stats_json TEXT,
    classic_item_level INTEGER,
    diff_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_slot ON items(slot);
CREATE INDEX IF NOT EXISTS idx_items_change_status ON items(change_status);
"""


def write_db(db_path: Path, meta: dict[str, str], items: dict[int, dict]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM items")
        conn.executemany(
            """
            INSERT INTO items (
                id, name, quality, item_class, item_subclass, inventory_type,
                slot, material, required_level, allowable_class_mask,
                item_level, change_status, provisional, stats_json,
                classic_stats_json, classic_item_level, diff_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    it["id"], it["name"], it["quality"], it["item_class"], it["item_subclass"],
                    it["inventory_type"], it["slot"], it["material"], it["required_level"],
                    it["allowable_class_mask"], it["item_level"], it["change_status"],
                    int(it["provisional"]), json.dumps(it["stats"]),
                    json.dumps(it["classic_stats"]) if "classic_stats" in it else None,
                    it.get("classic_item_level"),
                    json.dumps(it["diff"]) if it.get("diff") else None,
                )
                for it in items.values()
            ],
        )
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            list(meta.items()),
        )
        conn.commit()
    finally:
        conn.close()
