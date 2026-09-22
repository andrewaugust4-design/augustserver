"""Forever stat math: stat-type ids -> display name/category, and the
StatPercentEditor x RandPropPoints budget formula.

Everything here was checked against live data on 2026-09-22 (Forever
1.60.1.69913 vs Classic Era 1.15.9.69722) rather than taken on faith:

- Budget column: fitted per InventoryType x quality against Classic Era's
  stored stat values on items both builds share (ingest/validate.py re-runs
  this check on every ingest). Quality picks Good/Superior/Epic; slot picks
  column index 0-4, same layout retail uses.
- Stat ids follow retail's ItemModType enum. Each non-primary id was matched
  against the Classic Era equip spell (ItemEffect -> SpellEffect aura type,
  school mask, base points + 1) or Resistances_N column on shared items —
  e.g. 51/55 = Fire/Nature resistance on Thunderfury, 85 = Fire spell damage
  on Inferno Robe/Scorching Sash. Ids 83-89 are spell damage in school-mask
  order (phys, holy, fire, nature, frost, shadow, arcane); 85-89 are confirmed,
  84 (holy) is inferred from that order, 83 has too little data to name.
- Hit/crit/dodge/parry/block are *ratings* in Forever (Classic's +1% hit
  shows up as ~10), so they're labelled as ratings; converting a rating back
  to a % needs a combat-rating table we don't have.

Anything not in STAT_INFO renders as "Stat <id>" and is logged by ingest.
"""

from __future__ import annotations

PRIMARY = "primary"          # inline "+N Stat"
RESISTANCE = "resistance"    # inline "+N <School> Resistance"
EQUIP = "equip"              # under an "Equip:" line

# id -> (display name, category)
STAT_INFO: dict[int, tuple[str, str]] = {
    0: ("Mana", PRIMARY),
    1: ("Health", PRIMARY),
    3: ("Agility", PRIMARY),
    4: ("Strength", PRIMARY),
    5: ("Intellect", PRIMARY),
    6: ("Spirit", PRIMARY),
    7: ("Stamina", PRIMARY),
    50: ("Armor", PRIMARY),  # bonus armor on top of the item's base armor
    12: ("defense", EQUIP),
    13: ("dodge rating", EQUIP),
    14: ("parry rating", EQUIP),
    15: ("block rating", EQUIP),
    31: ("hit rating", EQUIP),
    32: ("critical strike rating", EQUIP),
    36: ("haste rating", EQUIP),
    37: ("expertise rating", EQUIP),
    38: ("attack power", EQUIP),
    39: ("ranged attack power", EQUIP),
    41: ("healing", EQUIP),
    42: ("spell damage", EQUIP),
    43: ("mana per 5 sec.", EQUIP),
    44: ("armor penetration rating", EQUIP),
    45: ("spell power", EQUIP),
    46: ("health per 5 sec.", EQUIP),
    47: ("spell penetration", EQUIP),
    48: ("block value", EQUIP),
    51: ("Fire Resistance", RESISTANCE),
    52: ("Frost Resistance", RESISTANCE),
    53: ("Holy Resistance", RESISTANCE),
    54: ("Shadow Resistance", RESISTANCE),
    55: ("Nature Resistance", RESISTANCE),
    56: ("Arcane Resistance", RESISTANCE),
    84: ("holy spell damage", EQUIP),
    85: ("fire spell damage", EQUIP),
    86: ("nature spell damage", EQUIP),
    87: ("frost spell damage", EQUIP),
    88: ("shadow spell damage", EQUIP),
    89: ("arcane spell damage", EQUIP),
    90: ("two-handed axes skill", EQUIP),
    91: ("two-handed maces skill", EQUIP),
    96: ("daggers skill", EQUIP),
    103: ("swords skill", EQUIP),
    112: ("herbalism", EQUIP),
    113: ("mining", EQUIP),
    117: ("fishing", EQUIP),
    119: ("fire spell penetration", EQUIP),
    121: ("frost spell penetration", EQUIP),
    124: ("All Resistances", RESISTANCE),
    127: ("attack power vs. demons", EQUIP),
    128: ("attack power vs. undead", EQUIP),
    131: ("attack power vs. beasts", EQUIP),
    136: ("spell damage vs. undead", EQUIP),
}

PRIMARY_STAT_IDS = frozenset(k for k, (_, cat) in STAT_INFO.items() if cat == PRIMARY and k != 50)
RESISTANCE_STAT_IDS = frozenset((51, 52, 53, 54, 55, 56))

# Classic Era keeps resistances in ItemSparse.Resistances_N (N = spell school:
# 1 holy, 2 fire, 3 nature, 4 frost, 5 shadow, 6 arcane) instead of the stat
# array; map them onto the same stat ids Forever uses so the two compare.
CLASSIC_RESISTANCE_COLUMN_TO_STAT = {1: 53, 2: 51, 3: 55, 4: 52, 5: 54, 6: 56}


def stat_info(stat_id: int) -> tuple[str, str]:
    return STAT_INFO.get(stat_id, (f"Stat {stat_id}", EQUIP))


# ── Budget (RandPropPoints) selection ──────────────────────────────────────
# InventoryType -> RandPropPoints column index.
BUDGET_SLOT_INDEX = {
    1: 0, 5: 0, 7: 0, 17: 0, 20: 0,          # head, chest, legs, two-hand, robe
    3: 1, 6: 1, 8: 1, 10: 1, 12: 1,          # shoulder, waist, feet, hands, trinket
    2: 2, 9: 2, 11: 2, 14: 2, 16: 2, 23: 2,  # neck, wrist, finger, shield, back, held in off-hand
    13: 3, 21: 3, 22: 3,                     # one-hand, main hand, off hand
    15: 4, 25: 4, 26: 4, 28: 4,              # bow, thrown, gun/crossbow/wand, relic
}
# OverallQualityID -> RandPropPoints column family. Poor/common items with
# stats use the Good budget; legendary/artifact use Epic.
BUDGET_QUALITY_COLUMN = {0: "Good", 1: "Good", 2: "Good", 3: "Superior", 4: "Epic", 5: "Epic", 6: "Epic"}


def stat_budget(budgets: dict[int, dict], item_level: int, quality: int, inventory_type: int) -> int | None:
    """The item's stat budget, or None if its level/quality/slot has none."""
    row = budgets.get(item_level)
    idx = BUDGET_SLOT_INDEX.get(inventory_type)
    family = BUDGET_QUALITY_COLUMN.get(quality)
    if row is None or idx is None or family is None:
        return None
    return int(row[f"{family}_{idx}"])


def stat_value(weight_bp: int, budget: int) -> int:
    """StatPercentEditor (basis points) x budget, rounded to nearest (half
    away from zero). The Classic Era cross-check has no exact .5 cases, so
    this and banker's rounding score identically; half-away is just the
    conventional choice and doesn't depend on Python's round() semantics."""
    raw = weight_bp * budget / 10000
    return int(raw + 0.5) if raw >= 0 else -int(-raw + 0.5)
