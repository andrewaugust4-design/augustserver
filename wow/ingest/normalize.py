"""Item.csv + ItemSparse.csv -> normalized item records, Forever-vs-Classic-Era
diff, and the SQLite write. See common/constants.py, common/proficiency.py
and common/stats.py for the verified enum values and stat formula this relies on.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from common.armor import item_armor
from common.constants import (
    ARMOR_MATERIAL_NAMES,
    INVTYPE_TO_SLOT,
    ITEM_CLASS_ARMOR,
    ITEM_CLASS_WEAPON,
    WEAPON_SUBCLASS_NAMES,
)
from common.stats import CLASSIC_RESISTANCE_COLUMN_TO_STAT, stat_budget, stat_info, stat_value
from . import character_data, downrank, quests, talents
from .wago_client import read_csv


def load_budgets(randproppoints_csv: Path) -> dict[int, dict]:
    """RandPropPoints rows keyed by item level (the table's ID)."""
    return {int(row["ID"]): row for row in read_csv(randproppoints_csv)}


def load_armor_tables(build_dir: Path) -> dict[str, dict[int, dict]]:
    """The four armor DB2s common/armor.py computes base armor from."""
    return {
        "total": {int(r["ItemLevel"]): r for r in read_csv(build_dir / "ItemArmorTotal.csv")},
        "quality": {int(r["ID"]): r for r in read_csv(build_dir / "ItemArmorQuality.csv")},
        "location": {int(r["ID"]): r for r in read_csv(build_dir / "ArmorLocation.csv")},
        "shield": {int(r["ItemLevel"]): r for r in read_csv(build_dir / "ItemArmorShield.csv")},
    }


UNIQUE_EQUIPPED_FLAG = 0x80000  # ItemSparse.Flags_0; MaxCount == 1 ("Unique") also blocks a second copy


def _race_mask(row: dict) -> int:
    """AllowableRace as one signed 64-bit int. Forever splits the mask into
    two signed 32-bit halves (AllowableRace_0/_1) because the Skyborne races
    sit on PlayableRaceBit 32/33; -1 in both = every race. Kept signed so it
    fits SQLite's INTEGER; `mask & (1 << bit)` still works for bits 0-63."""
    if "AllowableRace_0" not in row:
        return int(row.get("AllowableRace") or -1)  # Classic Era: single 32-bit column
    lo = int(row["AllowableRace_0"] or -1) & 0xFFFFFFFF
    hi = int(row.get("AllowableRace_1") or -1) & 0xFFFFFFFF
    mask = (hi << 32) | lo
    return mask - (1 << 64) if mask >= 1 << 63 else mask


def _stat(stat_id: int, value: int | None, **extra) -> dict:
    name, category = stat_info(stat_id)
    return {"stat_id": stat_id, "name": name, "category": category, "value": value, **extra}

DIFF_FIELDS = ("name", "quality", "slot", "material", "required_level", "item_level")


def parse_build(item_csv: Path, itemsparse_csv: Path, *, budgets: dict[int, dict] | None = None,
                armor_tables: dict[str, dict[int, dict]] | None = None) -> dict[int, dict]:
    """Parse one build's Item + ItemSparse CSVs into {item_id: record}.

    Classic Era (`budgets` is None) stores real stat numbers in
    StatModifier_bonusAmount_N, plus resistances in Resistances_N. Forever
    (`budgets` = load_budgets(...)) has no bonusAmount columns — only
    StatPercentEditor_N, a weight in basis points that's multiplied by the
    item's RandPropPoints budget (see common/stats.py). Forever stats keep
    the raw weight as `weight_bp` for debugging; value is None when the
    item has no budget (unknown slot/quality/level).

    Base armor likewise: Classic Era stores it (Resistances_0), Forever
    (`armor_tables` = load_armor_tables(...)) computes it — common/armor.py.
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

        item_level = int(row.get("ItemLevel") or 0)
        quality = int(row.get("OverallQualityID") or 0)
        budget = stat_budget(budgets, item_level, quality, inventory_type) if budgets is not None else None

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
            if budgets is None:
                amount = int(row.get(f"StatModifier_bonusAmount_{i}") or 0)
                if amount:
                    stats.append(_stat(stat_id, amount))
            else:
                weight = int(row.get(f"StatPercentEditor_{i}") or 0)
                if weight:
                    value = stat_value(weight, budget) if budget is not None else None
                    stats.append(_stat(stat_id, value, weight_bp=weight))
        if budgets is None:
            for col, stat_id in CLASSIC_RESISTANCE_COLUMN_TO_STAT.items():
                amount = int(row.get(f"Resistances_{col}") or 0)
                if amount:
                    stats.append(_stat(stat_id, amount))

        armor = None
        if item_class == ITEM_CLASS_ARMOR:
            if armor_tables is None:
                armor = int(row.get("Resistances_0") or 0) or None
            else:
                armor = item_armor(armor_tables, quality=quality, item_level=item_level,
                                   inventory_type=inventory_type, item_subclass=item_subclass)

        if item_class == ITEM_CLASS_WEAPON:
            material = WEAPON_SUBCLASS_NAMES.get(item_subclass)
        else:
            material = ARMOR_MATERIAL_NAMES.get(item_subclass)  # None for misc/shield/relic slots

        item_id = int(row["ID"])
        unique = int(row.get("MaxCount") or 0) == 1 or bool(int(row.get("Flags_0") or 0) & UNIQUE_EQUIPPED_FLAG)
        out[item_id] = {
            "id": item_id,
            "name": name,
            "quality": quality,
            "item_class": item_class,
            "item_subclass": item_subclass,
            "inventory_type": inventory_type,
            "slot": slot,
            "material": material,
            "required_level": int(row.get("RequiredLevel") or 0),
            "allowable_class_mask": allowable_class_mask,
            "allowable_race_mask": _race_mask(row),
            "unique_equipped": unique,
            "item_set": int(row.get("ItemSet") or 0),
            "item_level": item_level,
            "stats": stats,
            "armor": armor,
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
            record["classic_armor"] = c_item["armor"]
        merged[item_id] = record
    return merged


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

DROP TABLE IF EXISTS items;
CREATE TABLE items (
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
    allowable_race_mask INTEGER NOT NULL,
    unique_equipped INTEGER NOT NULL,
    item_level INTEGER NOT NULL,
    change_status TEXT NOT NULL,
    stats_json TEXT NOT NULL,
    classic_stats_json TEXT,
    classic_item_level INTEGER,
    diff_json TEXT,
    armor INTEGER,
    classic_armor INTEGER,
    effects_json TEXT,
    item_set INTEGER
);

CREATE INDEX idx_items_slot ON items(slot);
CREATE INDEX idx_items_change_status ON items(change_status);

-- Item sets, from the Forever build (see ingest/effects.py).
DROP TABLE IF EXISTS item_sets;
CREATE TABLE item_sets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    is_placeholder INTEGER NOT NULL,
    members_json TEXT NOT NULL,
    bonuses_json TEXT NOT NULL
);

-- Forever build's RandPropPoints (stat budget per item level), kept for
-- reference/debugging (item stat values are already computed at ingest).
DROP TABLE IF EXISTS rand_prop_points;
CREATE TABLE rand_prop_points (
    item_level INTEGER PRIMARY KEY,
    budgets_json TEXT NOT NULL
);
"""

BUDGET_COLUMNS = [f"{family}_{i}" for family in ("Epic", "Superior", "Good") for i in range(5)]


def write_db(db_path: Path, meta: dict[str, str], items: dict[int, dict], budgets: dict[int, dict],
             character: dict[str, list[tuple]] | None = None, item_sets: list[tuple] | None = None,
             quest_rows: list[tuple] | None = None, talent_rows: list[tuple] | None = None,
             downrank_rows: list[tuple] | None = None) -> None:
    """Rebuild the DB contents in a single transaction, so the running app
    never sees a half-written items table."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("BEGIN")
        for statement in SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.executemany(
            """
            INSERT INTO items (
                id, name, quality, item_class, item_subclass, inventory_type,
                slot, material, required_level, allowable_class_mask,
                allowable_race_mask, unique_equipped,
                item_level, change_status, stats_json,
                classic_stats_json, classic_item_level, diff_json,
                armor, classic_armor, effects_json, item_set
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    it["id"], it["name"], it["quality"], it["item_class"], it["item_subclass"],
                    it["inventory_type"], it["slot"], it["material"], it["required_level"],
                    it["allowable_class_mask"], it["allowable_race_mask"], int(it["unique_equipped"]),
                    it["item_level"], it["change_status"],
                    json.dumps(it["stats"]),
                    json.dumps(it["classic_stats"]) if "classic_stats" in it else None,
                    it.get("classic_item_level"),
                    json.dumps(it["diff"]) if it.get("diff") else None,
                    it["armor"], it.get("classic_armor"),
                    json.dumps(it["effects"]) if it.get("effects") else None,
                    it["item_set"] or None,
                )
                for it in items.values()
            ],
        )
        conn.executemany(
            "INSERT INTO rand_prop_points (item_level, budgets_json) VALUES (?, ?)",
            [
                (ilvl, json.dumps({col: int(row[col]) for col in BUDGET_COLUMNS}))
                for ilvl, row in sorted(budgets.items())
            ],
        )
        if character is not None:
            character_data.write_tables(conn, character)
        if item_sets:
            conn.executemany("INSERT INTO item_sets VALUES (?, ?, ?, ?, ?)", item_sets)
        if quest_rows is not None:
            quests.write_tables(conn, quest_rows)
        if talent_rows is not None:
            talents.write_tables(conn, talent_rows)
        if downrank_rows is not None:
            downrank.write_tables(conn, downrank_rows)
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            list(meta.items()),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
