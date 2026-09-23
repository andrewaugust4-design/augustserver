"""Item effects (Use: / Equip: / Chance on hit:) and item sets, rendered to
tooltip text at ingest time. See common/spelltext.py for the renderer.

Which build each piece comes from (checked 2026-09-22 on 1.60.1.69913 vs
Era 1.15.9.69722):

- **Item → effect links.** Classic Era's ItemEffect carries ParentItemID
  directly, so it's the source for carryover items. Forever links through
  ItemXItemEffect, which community importers treat as unverified. We use it
  only as a filter, and it checks out: on every carryover item where both
  builds link effects, Forever's links are a subset of Era's. What Forever
  dropped is *only* Equip stat auras (+spell damage, crit, hit, mp5, AP,
  defense…), which Forever turned into real item stats, already shown in the
  stat lines. So carryover effects = Era links that Forever still has.
- **New items** have only Forever links. Those whose spell text is readable
  are shown. A linked spell with no readable text becomes an "effect not yet
  revealed" line, and fills in when the data appears.
- **Text** is rendered from Forever's spell tables when the spell is readable
  there, since that's the current value: Forever retuned some procs, e.g.
  the Frost Bolt proc now does 40 to 60, not 20 to 30. Otherwise it uses
  Classic Era's tables.
- **Sets.** Forever's ItemSet/ItemSetSpell are fully readable (every bonus
  spell has text), and Forever changed 18 carryover sets' bonuses (e.g.
  Imperial Plate's five thresholds, 2/3/4/5/6). So sets come from Forever,
  with Era text only as a fallback for unreadable bonus spells.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from common.spelltext import SpellBook

from .wago_client import read_csv

ERA_TABLES = ("ItemEffect", "Spell", "SpellName", "SpellEffect", "SpellMisc", "SpellDuration",
              "SpellAuraOptions", "SpellRadius")
FOREVER_TABLES = (*ERA_TABLES, "ItemXItemEffect", "ItemSet", "ItemSetSpell")

TRIGGER_PREFIX = {0: "Use:", 1: "Equip:", 2: "Chance on hit:"}
NOT_REVEALED = "effect not yet revealed — encrypted in the beta client until looted"
SET_NOT_REVEALED = "bonus not yet revealed — encrypted in the beta client"

# Set names that look like internal placeholders rather than real names.
PLACEHOLDER_NAME = re.compile(r"(?i)(\bPH\b|\bTBD\b|\bDNT\b|placeholder|\btest\b|^zz|\bnyi\b|^\[|\bset \d+$|\bunused\b)")

# SPELL_EFFECT_APPLY_AURA auras that are a flat, always-on stat → sheet key.
APPLY_AURA = 6
_ATTR_BY_MISC = {0: "strength", 1: "agility", 2: "stamina", 3: "intellect", 4: "spirit"}
_SCHOOL_BITS = {1: "armor", 2: "holy", 4: "fire", 8: "nature", 16: "frost", 32: "shadow", 64: "arcane"}
_SIMPLE_AURAS = {
    99: "melee_ap", 124: "ranged_ap", 34: "health", 52: "melee_crit", 290: "melee_crit", 57: "spell_crit",
    49: "dodge", 47: "parry", 51: "block", 158: "block_value",
    54: "hit %", 55: "spell hit %", 13: "spell damage", 135: "healing", 85: "mana per 5 sec.",
    161: "health per 5 sec.", 123: "spell penetration",
}


def _links_era(build_dir: Path) -> dict[int, list[tuple[int, int, int]]]:
    out: dict[int, list] = defaultdict(list)
    for r in read_csv(build_dir / "ItemEffect.csv"):
        out[int(r["ParentItemID"])].append((int(r["LegacySlotIndex"]), int(r["TriggerType"]), int(r["SpellID"])))
    return out


def _links_forever(build_dir: Path) -> dict[int, list[tuple[int, int, int]]]:
    effects = {r["ID"]: r for r in read_csv(build_dir / "ItemEffect.csv")}
    out: dict[int, list] = defaultdict(list)
    for r in read_csv(build_dir / "ItemXItemEffect.csv"):
        e = effects.get(r["ItemEffectID"])
        if e is not None:
            out[int(r["ItemID"])].append((int(e["LegacySlotIndex"]), int(e["TriggerType"]), int(e["SpellID"])))
    return out


def _render(spell_id: int, forever: SpellBook, era: SpellBook) -> tuple[str | None, str | None]:
    """(text, source build) — Forever's text when readable, else Era's."""
    text = forever.render(spell_id)
    if text:
        return text, "forever"
    text = era.render(spell_id)
    return (text, "classic_era") if text else (None, None)


def build_item_effects(items: dict[int, dict], era_dir: Path, forever_dir: Path) -> tuple[dict[int, list[dict]], dict]:
    era_book, forever_book = SpellBook(era_dir), SpellBook(forever_dir)
    era_links, forever_links = _links_era(era_dir), _links_forever(forever_dir)
    out: dict[int, list[dict]] = {}
    stats = defaultdict(int)
    for item_id, item in items.items():
        f_keys = {(t, s) for _, t, s in forever_links.get(item_id, [])}
        if item["change_status"] == "new":
            links = sorted(forever_links.get(item_id, []))
        else:
            era = sorted(era_links.get(item_id, []))
            links = [l for l in era if (l[1], l[2]) in f_keys]
            stats["carry_links_dropped_as_stats"] += len(era) - len(links)
            era_keys = {(t, s) for _, t, s in era}
            # Effects Forever added to a carryover item: shown only if readable.
            links += [l for l in sorted(forever_links.get(item_id, [])) if (l[1], l[2]) not in era_keys
                      and forever_book.has_text(l[2])]
        effects = []
        for _, trigger, spell_id in links:
            if trigger not in TRIGGER_PREFIX:
                continue
            text, source = _render(spell_id, forever_book, era_book)
            if text is None:
                if item["change_status"] != "new":
                    continue  # carryover spells without text are helper spells the game doesn't show either
                effects.append({"prefix": TRIGGER_PREFIX[trigger], "text": NOT_REVEALED, "spell_id": spell_id,
                                "revealed": False, "source": None})
                stats["new_unrevealed"] += 1
                continue
            effects.append({"prefix": TRIGGER_PREFIX[trigger], "text": text, "spell_id": spell_id,
                            "revealed": True, "source": source})
            stats[f"{item['change_status'] == 'new' and 'new' or 'carry'}_revealed"] += 1
        if effects:
            out[item_id] = effects
    stats["items_with_effects"] = len(out)
    return out, dict(stats)


def _flat_stats(spell_id: int, book: SpellBook) -> list[dict] | None:
    """The spell as flat sheet stats, or None if any effect isn't one
    (procs, conditional bonuses, "+X AP vs Undead"…): display-only."""
    effects = book.effects.get(spell_id)
    if not effects:
        return None
    out = []
    for idx, eff in sorted(effects.items()):
        if int(eff["Effect"]) != APPLY_AURA:
            return None
        aura, misc = int(eff["EffectAura"]), int(eff.get("EffectMiscValue_0") or 0)
        value = book.value(spell_id, idx)
        value = int(value) if value == int(value) else value
        if aura == 29:  # MOD_STAT
            keys = list(_ATTR_BY_MISC.values()) if misc == -1 else [_ATTR_BY_MISC.get(misc)]
        elif aura == 22:  # MOD_RESISTANCE (school mask)
            keys = [name for bit, name in _SCHOOL_BITS.items() if misc & bit and name != "holy"]
        elif aura == 30 and misc == 95:  # MOD_SKILL: Defense
            keys = ["defense"]
        elif aura == 35 and misc == 0:  # MOD_INCREASE_ENERGY: mana
            keys = ["mana"]
        elif aura in _SIMPLE_AURAS:
            keys = [_SIMPLE_AURAS[aura]]
        else:
            return None
        if not keys or None in keys:
            return None
        out += [{"key": k, "value": value} for k in keys]
    return out


def build_sets(items: dict[int, dict], era_dir: Path, forever_dir: Path) -> tuple[list[tuple], dict]:
    """Rows for the item_sets table, for every set a gear item belongs to."""
    import json

    era_book, forever_book = SpellBook(era_dir), SpellBook(forever_dir)
    set_rows = {int(r["ID"]): r for r in read_csv(forever_dir / "ItemSet.csv")}
    bonuses: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for r in read_csv(forever_dir / "ItemSetSpell.csv"):
        bonuses[int(r["ItemSetID"])].append((int(r["Threshold"]), int(r["SpellID"])))
    names = {int(r["ID"]): (r.get("Display_lang") or "", int(r.get("OverallQualityID") or 0))
             for r in read_csv(forever_dir / "ItemSparse.csv")}

    used = {it["item_set"] for it in items.values() if it.get("item_set")}
    rows, stats = [], defaultdict(int)
    for set_id in sorted(used):
        row = set_rows.get(set_id)
        if row is None:
            stats["sets_missing"] += 1
            continue
        name = row["Name_lang"] or f"Set {set_id}"
        members = [int(row[f"ItemID_{i}"]) for i in range(17) if int(row.get(f"ItemID_{i}") or 0)]
        member_rows = [{"id": m, "name": names.get(m, ("", 0))[0] or f"Item {m}", "quality": names.get(m, ("", 0))[1]}
                       for m in members]
        bonus_rows = []
        for threshold, spell_id in sorted(bonuses.get(set_id, [])):
            text, source = _render(spell_id, forever_book, era_book)
            book = forever_book if spell_id in forever_book.effects else era_book
            bonus_rows.append({
                "threshold": threshold, "spell_id": spell_id,
                "text": text or SET_NOT_REVEALED, "revealed": text is not None, "source": source,
                "stats": _flat_stats(spell_id, book),
            })
            stats["bonuses_revealed" if text else "bonuses_unrevealed"] += 1
            stats["bonuses_flat" if bonus_rows[-1]["stats"] else "bonuses_display_only"] += 1
        placeholder = bool(PLACEHOLDER_NAME.search(name)) or not row["Name_lang"]
        stats["placeholder_names"] += placeholder
        rows.append((set_id, name, int(placeholder), json.dumps(member_rows), json.dumps(bonus_rows)))
    stats["sets"] = len(rows)
    return rows, dict(stats)
