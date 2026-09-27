"""Macro Builder data: each class's castable spells (for the spell dropdowns)
and a list of usable consumables (for ``/use`` suggestions).

Only the spell *names* matter to a macro; the rest is hints for the UI.
The macro syntax itself is hand-encoded in the page (app/static/macros/),
not datamined.

Sources (checked 2026-09-27, Forever 1.60.1.70009):

- **Which spells.** Same trainer set as the Downrank Calculator:
  `SkillLineAbility` rows on a class skill line with AcquireMethod 0/2
  (AcquireMethod 3 is the Season of Discovery rune copies). Dropped: passive
  spells (`SpellMisc.Attributes_0` & 0x40), "(Passive)" / "… Effect" helper
  spells, and rows without a `SpellLevels` level. That last rule removes the
  talent proc spells that sit on the class line unflagged (Blessed Recovery,
  Focused Casting). Talent spells are added from the talent data: a talent
  whose spell isn't passive is an active ability (Penance, Swiftmend,
  Mortal Strike…). A spell that shares its name with a passive talent is a
  talent's proc, not something you cast, so it's dropped too.
- **One entry per name**, with the highest rank's ID, the rank count (for
  `Spell(Rank N)` downranking) and the level of rank 1.
- **Target hint.** From the highest rank's `SpellEffect.ImplicitTarget`:
  87 (destination) = ground-targeted, 21/56/57 = friendly, 6 = enemy,
  25 = either (Holy Shock, Penance), otherwise self. It only orders the
  template dropdowns; every spell stays pickable.
- **Consumables.** `Item` ClassID 0 with an English name, test/placeholder
  names filtered out.
- **Hunter pet abilities** (Bite, Claw, Growl… from Beast Training) are the
  "teach pet" spells, `Attributes_4` & 0x8000. They're kept and tagged `pet`,
  because `/cast Bite` from the hunter's macro commands the pet.
- **Procs** that look castable in the data (a triggered buff with its own
  level) are listed by name in PROC_NAMES. Filtering on "triggered by
  another spell" would also drop Backstab, Bear Form and the stances.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from pathlib import Path

from .downrank import _class_spells
from .talents import _class_names, _i, _slug
from .wago_client import read_csv

log = logging.getLogger("wow.ingest")

FOREVER_TABLES = ("SkillLineAbility", "SkillLine", "SkillRaceClassInfo", "SpellMisc", "SpellLevels", "SpellEffect",
                  "Spell", "SpellName", "ChrClasses", "Item", "ItemSparse")

ATTR0_PASSIVE = 0x40
ATTR4_PET_ABILITY = 0x8000
HUNTER = 3
PROC_NAMES = {"Chilled", "Clearcasting", "Lightwell Renew"}
CHANNELED_ATTR1 = 0x4 | 0x40
SHAPESHIFT_AURA = 36
TARGET_GROUND = {87}
TARGET_HELP = {21, 56, 57}
TARGET_HARM = {6}
TARGET_ANY = {25}
HELPER_NAME = re.compile(r"(\(Passive\d*\)| Effect| Passive)$")
ITEM_CONSUMABLE = 0
JUNK_ITEM = re.compile(r"^(?:\W|zz|test|deprecated|old|unused|monster|qa |\[ph\]|ph |placeholder|craftsman's writ|.*\bDND\b)|test\b",
                       re.IGNORECASE)

# Anchors checked every ingest: (class slug, spell, expected target hint).
ANCHORS = [("priest", "Flash Heal", "help"), ("priest", "Penance", "any"), ("mage", "Blizzard", "ground"),
           ("mage", "Frostbolt", "harm"), ("druid", "Cat Form", "self"), ("warrior", "Mortal Strike", "harm"),
           ("rogue", "Kick", "harm")]


def _target(effects: list[dict]) -> str:
    targets = {_i(e[k]) for e in effects for k in ("ImplicitTarget_0", "ImplicitTarget_1")}
    if targets & TARGET_GROUND:
        return "ground"
    help_, harm = bool(targets & (TARGET_HELP | TARGET_ANY)), bool(targets & (TARGET_HARM | TARGET_ANY))
    if help_ and harm:
        return "any"
    return "help" if help_ else "harm" if harm else "self"


def _rank_no(subtext: str) -> int | None:
    m = re.match(r"Rank (\d+)", subtext or "")
    return int(m.group(1)) if m else None


def build_macros(forever_dir: Path, talent_rows: list[tuple]) -> tuple[list[tuple], list[str], dict, list[str]]:
    """Rows for macro_classes, the consumable name list, meta counts, and anchor problems."""
    name = {_i(r["ID"]): r["Name_lang"] for r in read_csv(forever_dir / "SpellName.csv")}
    subtext = {_i(r["ID"]): r.get("NameSubtext_lang") or "" for r in read_csv(forever_dir / "Spell.csv")}
    misc = {_i(r["SpellID"]): r for r in read_csv(forever_dir / "SpellMisc.csv") if _i(r["DifficultyID"]) == 0}
    level = {_i(r["SpellID"]): _i(r["SpellLevel"]) for r in read_csv(forever_dir / "SpellLevels.csv")
             if _i(r["DifficultyID"]) == 0}
    effects: dict[int, list[dict]] = defaultdict(list)
    for r in read_csv(forever_dir / "SpellEffect.csv"):
        if _i(r["DifficultyID"]) == 0:
            effects[_i(r["SpellID"])].append(r)

    def passive(sid: int) -> bool:
        return bool(_i(misc.get(sid, {}).get("Attributes_0")) & ATTR0_PASSIVE)

    # class id → (active talent spell ids, passive talent names)
    talent_active: dict[int, set[int]] = defaultdict(set)
    talent_passive: dict[int, set[str]] = defaultdict(set)
    for _slug_, class_id, _name, _count, data_json in talent_rows:
        for t in json.loads(data_json)["talents"]:
            if passive(t["spell_id"]):
                talent_passive[class_id].add(t["name"])
            else:
                talent_active[class_id].add(t["spell_id"])

    class_names = _class_names(forever_dir)
    stats = defaultdict(int)
    rows = []
    for cls, trainer_ids in sorted(_class_spells(forever_dir).items()):
        by_name: dict[str, dict] = {}
        talent_ids = talent_active.get(cls, set())
        for sid in [*trainer_ids, *sorted(talent_ids)]:
            spell_name = name.get(sid)
            is_talent = sid in talent_ids
            if (not spell_name or passive(sid) or HELPER_NAME.search(spell_name) or subtext.get(sid) == "Passive"
                    or spell_name in PROC_NAMES):
                continue
            if not is_talent and (not level.get(sid) or spell_name in talent_passive[cls]):
                continue
            entry = by_name.setdefault(spell_name, {"ids": {}, "talent": False})
            entry["talent"] |= is_talent
            entry["ids"][_rank_no(subtext.get(sid)) or 1] = sid
        spells = []
        for spell_name, entry in by_name.items():
            ranks = entry["ids"]
            top = ranks[max(ranks)]
            m = misc.get(top, {})
            spells.append({
                "name": spell_name, "id": top, "ranks": max(ranks) if len(ranks) > 1 else 1,
                "level": min((level.get(s) or 0 for s in ranks.values()), default=0),
                "icon": _i(m.get("SpellIconFileDataID")) or None,
                "target": _target(effects.get(top, [])),
                "form": any(_i(e["EffectAura"]) == SHAPESHIFT_AURA for e in effects.get(top, []))
                        or subtext.get(top) == "Shapeshift",
                "channeled": bool(_i(m.get("Attributes_1")) & CHANNELED_ATTR1),
                "talent": entry["talent"],
                "pet": cls == HUNTER and bool(_i(m.get("Attributes_4")) & ATTR4_PET_ABILITY),
            })
        spells.sort(key=lambda s: (s["level"], s["name"]))
        slug = _slug(class_names.get(cls, str(cls)))
        rows.append((slug, cls, class_names.get(cls, str(cls)), len(spells),
                     json.dumps({"id": cls, "slug": slug, "name": class_names.get(cls, str(cls)), "spells": spells})))
        stats["classes"] += 1
        stats["spells"] += len(spells)
        stats["talent_spells"] += sum(s["talent"] for s in spells)
        stats["ground"] += sum(s["target"] == "ground" for s in spells)
        stats["pet"] += sum(s["pet"] for s in spells)

    consumables = _consumables(forever_dir)
    stats["consumables"] = len(consumables)
    return rows, consumables, dict(stats), _check_anchors(rows)


def _consumables(forever_dir: Path) -> list[str]:
    ids = {_i(r["ID"]) for r in read_csv(forever_dir / "Item.csv") if _i(r["ClassID"]) == ITEM_CONSUMABLE}
    names = {r["Display_lang"].strip() for r in read_csv(forever_dir / "ItemSparse.csv")
             if _i(r["ID"]) in ids and r.get("Display_lang")}
    return sorted(n for n in names if n.isascii() and not JUNK_ITEM.search(n))


def _check_anchors(rows: list[tuple]) -> list[str]:
    spells = {r[0]: {s["name"]: s for s in json.loads(r[4])["spells"]} for r in rows}
    problems = []
    for slug, spell_name, expected in ANCHORS:
        got = spells.get(slug, {}).get(spell_name)
        if got is None:
            problems.append(f"anchor {slug}/{spell_name}: spell missing")
        elif got["target"] != expected:
            problems.append(f"anchor {slug}/{spell_name}: target {got['target']}, expected {expected}")
        else:
            log.info("Macro anchor OK: %s %s → %s", slug, spell_name, expected)
    return problems


SCHEMA = """
DROP TABLE IF EXISTS macro_classes;
CREATE TABLE macro_classes (
    slug TEXT PRIMARY KEY,
    class_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    spell_count INTEGER NOT NULL,
    data_json TEXT NOT NULL
);
DROP TABLE IF EXISTS macro_items;
CREATE TABLE macro_items (name TEXT PRIMARY KEY)
"""


def write_tables(conn, rows: list[tuple], consumables: list[str]) -> None:
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.executemany("INSERT INTO macro_classes VALUES (?, ?, ?, ?, ?)", rows)
    conn.executemany("INSERT INTO macro_items VALUES (?)", [(n,) for n in consumables])
