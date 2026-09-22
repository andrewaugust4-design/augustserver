"""Character sheet for the set builder: base race/class/level stats + gear,
then derived stats. Unbuffed and talentless by construction.

Where each number comes from (see reference/SOURCES.md for links):

- Base attributes / base health: NOT in the Forever client. Vanilla 1.12
  reference tables (reference/vanilla_*.json, from mangoszero, byte-identical
  to cmangos classic-db). Base health for Priest and Mage gets +10: wowsims'
  naked in-game measurements are exactly 10 higher at all four levels they
  measured (25/40/50/60), while every other class matches.
- New race/class combos (Human Hunter, Orc Mage, Dwarf Shaman, Undead
  Paladin, Gnome Priest): vanilla stats are exactly class base + a fixed
  per-race offset (true for every non-racial-% row checked), so these are
  derived as class base + that race's offset. Flagged "derived".
- Skyborne (both races): race offsets and racials are unknown until beta
  captures exist. Class base with zero offset, flagged "provisional".
- Base mana, crit per Agility, spell crit per Intellect: the Forever client's
  own PlayerExpectedStat table (identical to wowsims' measured values).
- Formulas (health/mana from Sta/Int, armor from Agi, attack power, block,
  parry, defense): vmangos StatSystem.cpp, which agrees with cmangos except
  caster-form Druid melee AP (vmangos: Str×2−20; cmangos adds level×3).
- Base melee crit / base dodge per class: vmangos (Nostalrius values) and
  wowsims measurements agree. Base spell crit: wowsims measurements (both
  emulators ship a table they themselves mark "MUST BE CHECKED").
- Dodge per Agility: same as crit per Agility, doubled for Rogue and Hunter
  (cmangos and wowsims agree).

Gear hit/crit/dodge/parry/block come through as Forever *ratings* with no
known rating→% table, so they're listed as gear bonuses, not folded into the
percentages.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .constants import CLASS_BY_SLUG

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "reference"

STR, AGI, STA, INT, SPI = "strength", "agility", "stamina", "intellect", "spirit"
ATTRS = (STR, AGI, STA, INT, SPI)
STAT_ID_ATTR = {4: STR, 3: AGI, 7: STA, 5: INT, 6: SPI}

# wowsims RaceOffsets (Human = 0). Matches every mangos row except the ones
# the Human Spirit / Gnome Expansive Mind +5% racials explain.
RACE_OFFSETS = {
    1: (0, 0, 0, 0, 0),
    2: (3, -3, 2, -3, 3),
    3: (2, -4, 3, -1, -1),
    4: (-3, 5, -1, 0, 0),
    5: (-1, -2, 1, -2, 5),
    6: (5, -5, 2, -5, 2),
    7: (-5, 3, -1, 3, 0),
    8: (1, 2, 1, -4, 1),
}
RACE_PCT = {1: (SPI, 1.05), 7: (INT, 1.05)}  # already baked into the reference rows
# Races whose offset is unaffected by a % racial, tried in order to recover class base.
OFFSET_SAFE_RACES = (2, 3, 4, 5, 6, 8)

BASE_HP_CORRECTION = {5: 10, 8: 10}  # Priest, Mage — see module docstring

RACIAL_RESIST = {3: {"frost": 10}, 4: {"nature": 10}, 5: {"shadow": 10}, 6: {"nature": 10}, 7: {"arcane": 10}}
RACIAL_NOTES = {
    4: "Quickness: +1% dodge",
    6: "Endurance: +5% health",
}
TAUREN_HEALTH_MULT = 1.05
NIGHT_ELF_DODGE = 1.0

# Class ids
WARRIOR, PALADIN, HUNTER, ROGUE, PRIEST, SHAMAN, MAGE, WARLOCK, DRUID = 1, 2, 3, 4, 5, 7, 8, 9, 11

BASE_MELEE_CRIT = {PALADIN: 0.7, PRIEST: 3.0, SHAMAN: 1.7, MAGE: 3.2, WARLOCK: 2.0, DRUID: 0.9}
BASE_DODGE = dict(BASE_MELEE_CRIT)
BASE_SPELL_CRIT = {PALADIN: 3.5, HUNTER: 3.6, PRIEST: 0.8, SHAMAN: 2.3, MAGE: 0.2, WARLOCK: 1.7, DRUID: 1.8}
DODGE_AGI_MULT = {ROGUE: 2.0, HUNTER: 2.0}
PARRY_CLASSES = {WARRIOR, PALADIN, HUNTER, ROGUE}
BLOCK_CLASSES = {WARRIOR, PALADIN, SHAMAN}

RESISTANCES = {51: "fire", 52: "frost", 55: "nature", 54: "shadow", 56: "arcane"}  # holy (53) isn't shown in vanilla
ALL_RESIST_STAT = 124
RATING_STATS = {31: "hit rating", 32: "crit rating", 13: "dodge rating", 14: "parry rating", 15: "block rating"}


@lru_cache(maxsize=1)
def _reference() -> tuple[dict, dict]:
    ls = json.loads((REFERENCE_DIR / "vanilla_player_levelstats.json").read_text())["rows"]
    cls = json.loads((REFERENCE_DIR / "vanilla_player_classlevelstats.json").read_text())["rows"]
    return {(r, c, l): tuple(v) for r, c, l, *v in ls}, {(c, l): hp for c, l, hp, _ in cls}


def _class_base(class_id: int, level: int) -> tuple[int, ...] | None:
    levelstats, _ = _reference()
    for race in OFFSET_SAFE_RACES:
        row = levelstats.get((race, class_id, level))
        if row:
            return tuple(v - o for v, o in zip(row, RACE_OFFSETS[race]))
    return None


def base_attributes(race_id: int, class_id: int, level: int) -> tuple[dict[str, int], str]:
    """({attr: value}, confidence) — confidence is reference / derived / provisional."""
    levelstats, _ = _reference()
    row = levelstats.get((race_id, class_id, level))
    if row:
        return dict(zip(ATTRS, row)), "reference"
    base = _class_base(class_id, level)
    if base is None:
        return dict.fromkeys(ATTRS, 0), "provisional"
    if race_id not in RACE_OFFSETS:
        return dict(zip(ATTRS, base)), "provisional"
    attrs = dict(zip(ATTRS, (b + o for b, o in zip(base, RACE_OFFSETS[race_id]))))
    if race_id in RACE_PCT:
        attr, mult = RACE_PCT[race_id]
        attrs[attr] = round(attrs[attr] * mult)
    return attrs, "derived"


def base_health(class_id: int, level: int) -> int:
    _, hp = _reference()
    return hp.get((class_id, level), 0) + BASE_HP_CORRECTION.get(class_id, 0)


def health_from_stamina(sta: float) -> float:
    return min(sta, 20) + max(sta - 20, 0) * 10


def mana_from_intellect(intellect: float) -> float:
    return min(intellect, 20) + max(intellect - 20, 0) * 15


def melee_ap(class_id: int, level: int, s: float, a: float) -> float:
    if class_id in (WARRIOR, PALADIN):
        return level * 3 + s * 2 - 20
    if class_id in (ROGUE, HUNTER):
        return level * 2 + s + a - 20
    if class_id == SHAMAN:
        return level * 2 + s * 2 - 20
    if class_id == DRUID:
        return s * 2 - 20  # caster form
    return s - 10


def ranged_ap(class_id: int, level: int, a: float) -> float:
    if class_id == HUNTER:
        return level * 2 + a * 2 - 10
    if class_id in (ROGUE, WARRIOR):
        return level + a - 10
    return a - 10


def compute_sheet(*, class_slug: str, race: dict, level: int, is_new_combo: bool,
                  class_level: dict | None, power_name: str, items: list[dict]) -> dict:
    """`race` is a races-table row; `class_level` a class_level row (None if
    the client has no row for that level); `items` the equipped item rows
    with parsed `stats`."""
    class_id = CLASS_BY_SLUG[class_slug]["class_id"]
    race_id = race["id"]
    base, confidence = base_attributes(race_id, class_id, level)
    if is_new_combo and confidence == "reference":
        confidence = "derived"  # can't happen today, but a new combo is never "reference"

    gear = dict.fromkeys(ATTRS, 0)
    armor_items = bonus_armor = gear_health = gear_mana = 0
    gear_ap = gear_rap = gear_defense = gear_block_value = 0
    resist = {name: RACIAL_RESIST.get(race_id, {}).get(name, 0) for name in RESISTANCES.values()}
    ratings: dict[str, int] = {}
    other: dict[str, int] = {}
    has_shield = False
    for item in items:
        armor_items += item.get("armor") or 0
        has_shield |= item.get("is_shield", False)
        for st in item["stats"]:
            sid, val = st["stat_id"], st["value"]
            if val is None:
                continue
            if sid in STAT_ID_ATTR:
                gear[STAT_ID_ATTR[sid]] += val
            elif sid == 50:
                bonus_armor += val
            elif sid == 1:
                gear_health += val
            elif sid == 0:
                gear_mana += val
            elif sid in RESISTANCES:
                resist[RESISTANCES[sid]] += val
            elif sid == ALL_RESIST_STAT:
                for k in resist:
                    resist[k] += val
            elif sid == 38:
                gear_ap += val
            elif sid == 39:
                gear_rap += val
            elif sid == 12:
                gear_defense += val
            elif sid == 48:
                gear_block_value += val
            elif sid in RATING_STATS:
                ratings[RATING_STATS[sid]] = ratings.get(RATING_STATS[sid], 0) + val
            else:
                other[st["name"]] = other.get(st["name"], 0) + val

    total = {k: base[k] + gear[k] for k in ATTRS}
    s, a, st_, i = total[STR], total[AGI], total[STA], total[INT]

    health = (base_health(class_id, level) + health_from_stamina(st_) + gear_health)
    if race_id == 6:
        health *= TAUREN_HEALTH_MULT
    has_mana = power_name == "Mana"
    mana = (class_level["base_mana"] + mana_from_intellect(i) + gear_mana) if has_mana and class_level else None

    crit_per_agi = (class_level["crit_per_agi"] * 100) if class_level else 0.0
    spell_crit_per_int = (class_level["spell_crit_per_int"] * 100) if class_level else 0.0
    defense = level * 5 + gear_defense
    def_bonus = gear_defense * 0.04  # vs. a same-level attacker, max skill = level × 5

    melee_crit = BASE_MELEE_CRIT.get(class_id, 0.0) + a * crit_per_agi
    dodge = BASE_DODGE.get(class_id, 0.0) + a * crit_per_agi * DODGE_AGI_MULT.get(class_id, 1.0) + def_bonus
    if race_id == 4:
        dodge += NIGHT_ELF_DODGE
    can_block = class_id in BLOCK_CLASSES and has_shield

    provisional_reasons = []
    if confidence == "provisional":
        provisional_reasons.append("Skyborne base stats and racials aren't known yet — class averages shown.")
    elif confidence == "derived":
        provisional_reasons.append("New race/class combo — base stats derived from the class base plus the race's usual offset.")
    if class_level is None:
        provisional_reasons.append("No client coefficients for this level.")

    return {
        "confidence": confidence,
        "provisional": provisional_reasons,
        "resource": power_name,
        "attributes": {k: {"base": base[k], "gear": gear[k], "total": total[k]} for k in ATTRS},
        "health": round(health),
        "mana": round(mana) if mana is not None else None,
        "armor": {"items": armor_items + bonus_armor, "agility": a * 2, "total": armor_items + bonus_armor + a * 2},
        "resistances": resist,
        "racials": RACIAL_NOTES.get(race_id),
        "combat": {
            "melee_ap": round(max(melee_ap(class_id, level, s, a), 0) + gear_ap),
            "ranged_ap": round(max(ranged_ap(class_id, level, a), 0) + gear_ap + gear_rap),
            "melee_crit": round(melee_crit, 2),
            "ranged_crit": round(melee_crit, 2),
            "spell_crit": round(BASE_SPELL_CRIT[class_id] + i * spell_crit_per_int, 2) if class_id in BASE_SPELL_CRIT else None,
            "dodge": round(dodge, 2),
            "parry": round(5.0 + def_bonus, 2) if class_id in PARRY_CLASSES else None,
            "block": round(5.0 + def_bonus, 2) if can_block else None,
            # vmangos GetShieldBlockValue: (block + Str/20 − 1), truncated. The shield's own
            # block value isn't in the Forever item data yet, so only gear "block value" counts.
            "block_value": int(max(gear_block_value + s / 20 - 1, 0)) if can_block else None,
            "defense": defense,
        },
        "gear_ratings": ratings,
        "gear_other": other,
    }
