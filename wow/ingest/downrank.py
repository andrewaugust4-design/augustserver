"""Downrank Calculator data: every rank of each class's spell-power-scaling
spells, with base values, mana, cast/duration and coefficients, plus a light
diff against Classic Era.

Sources (checked 2026-09-24, Forever 1.60.1.69977 vs Era 1.15.9.69722):

- **Which spells.** `SkillLineAbility` rows on a class skill line (SkillLine
  category 7, one class per `SkillRaceClassInfo`) with AcquireMethod 0
  (trainer) or 2 (known at creation: Smite, Fireball, Holy Light… rank 1).
  AcquireMethod 3 is a second copy of several rank chains
  (Renew 425268…, the Season of Discovery versions), which are skipped. Mana
  spells only (`SpellPower.PowerType` 0). Ranks are grouped by class + spell
  name and ordered by `Spell.NameSubtext_lang` ("Rank N").
- **Scaling effects.** Direct damage (Effect 2), heal (10) and health leech
  (9), plus auras for periodic damage (3), periodic heal (8), periodic leech
  (53) and absorb (69). Each is one "component" of the rank.
- **Coefficients come from the client.** Forever stores each effect's
  spell-power coefficient in `SpellEffect.EffectBonusCoefficient`. That
  includes the ones Classic kept server-side: PW:S 0.10, and Holy Light /
  Flash of Light as cast ÷ 3.5 (Era's client has 0 or a script effect for
  those). Periodic coefficients are per tick, so the total is per tick ×
  ticks (Renew 0.2 × 5 = 1.0).
- **No sub-level-20 penalty in Forever's client values.** Era bakes it in
  (Lesser Heal R1 0.123, Frostbolt R1 0.163); Forever doesn't (0.429 and
  0.407). The server could still apply it, so the calculator has a toggle
  that applies the classic penalty, 3.75% × (20 − spell level), on top.
  It's off by default so the numbers match the client.
- **Formula cross-check.** Each component's client coefficient is compared
  with the classic formulas:
  - direct: min(max(cast, 1.5), 3.5) / 3.5
  - over time: duration / 15
  - direct + over-time hybrids: the classic split, each part weighted by its
    share of the total
  - AoE: × 0.5
  - spells that both damage and heal: × 0.5
  - optionally the sub-20 penalty

  The counts are logged and stored in meta. The client wins where they
  disagree; mismatches are data, not errors (Forever retuned some spells,
  and snares/procs had their own penalties in classic).
- **Special cases** (Classic-measured, server-side in the client): used
  only when the client coefficient is 0, and marked `measured`. Ice Barrier
  10% is from the patch 2.3 notes ("1175 + 10%" before 2.3). Fire/Frost Ward
  10% is from community coefficient lists. Shadow Ward has no reliable
  figure and stays unscaled. PW:S, Holy Light and Flash of Light are listed
  too, as fallbacks and anchors, but Forever's client already has them.
- **Base values.** Forever stores the midpoint (`EffectBasePointsF`) and a
  relative spread (`Variance`), so min/max = mid × (1 ∓ variance/2). Era
  stores BasePoints + 1..DieSides. Values scale by `EffectRealPointsPerLevel`
  from the spell's level up to `SpellLevels.MaxLevel`, so they're shown at
  the spell's level and at a level-60 reference (marked provisional).
- **Mana.** `SpellPower.ManaCost`, or `PowerCostPct` × the class's base
  mana at 60 (client `PlayerExpectedStat`, from the character tables).
  Percentage costs are marked.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from pathlib import Path

from .talents import _class_names, _i, _slug
from .wago_client import read_csv

log = logging.getLogger("wow.ingest")

FOREVER_TABLES = ("SkillLineAbility", "SkillLine", "SkillRaceClassInfo", "SpellPower", "SpellCastTimes",
                  "SpellLevels", "SpellMisc", "SpellDuration", "SpellEffect", "Spell", "SpellName", "ChrClasses")
ERA_TABLES = ("SkillLineAbility", "SpellPower", "SpellCastTimes", "SpellLevels", "SpellMisc", "SpellDuration",
              "SpellEffect", "Spell", "SpellName")

CLASS_SKILL_CATEGORY = "7"
TRAINER = {"0", "2"}  # 0 = trainer, 2 = known at character creation (every class's rank-1 starters)
POWER_MANA = "0"
REF_LEVEL = 60

DIRECT = {2: "damage", 10: "heal", 9: "damage"}  # 9 = health leech (damage that heals the caster)
AURA_PERIODIC = {3: "damage", 8: "heal", 53: "damage"}
AURA_ABSORB = 69
APPLY_AURA_EFFECTS = {6, 27, 35}  # apply aura, persistent area aura, area aura
CHANNELED_ATTR1 = 0x4 | 0x40  # SPELL_ATTR1_CHANNELED_1 / _2
AURA_PERIODIC_TRIGGER = 23  # classic: e.g. Arcane Missiles ticks cast a missile spell
AURA_PERIODIC_DUMMY = 226  # retail-style: the tick is scripted (area trigger / companion spell)
SKIP_CLASSES = {3}  # Hunter: its mana spells scale with attack power, not spell power
# Seals and Lightning Shield: buffs whose damage depends on swings / hits
# taken over their duration, so "damage per mana" doesn't mean anything.
SKIP_NAME = re.compile(r"^(Seal of |Lightning Shield$)")
DUMMY_EFFECT = 3

# Classic-measured coefficients the client doesn't store (server-side).
# Used only where the Forever client's coefficient is 0.
SPECIAL = {
    "Power Word: Shield": ("flat", 0.10, "Classic measured 10%"),
    "Ice Barrier": ("flat", 0.10, "Classic 10% (patch 2.3 notes: 1175 + 10% before 2.3)"),
    "Fire Ward": ("flat", 0.10, "Classic measured 10%"),
    "Frost Ward": ("flat", 0.10, "Classic measured 10%"),
    "Holy Light": ("cast", None, "Server-side in Classic: cast ÷ 3.5"),
    "Flash of Light": ("cast", None, "Server-side in Classic: cast ÷ 3.5"),
}

# Anchors checked every ingest: (class id, spell, expected total coefficient per rank).
ANCHORS = [(5, "Greater Heal", 0.857), (5, "Renew", 1.0), (5, "Power Word: Shield", 0.10),
           (2, "Holy Light", 2.5 / 3.5), (2, "Flash of Light", 1.5 / 3.5)]
MATCH_TOL = 0.02


def _f(v) -> float:
    try:
        return float(v or 0)
    except ValueError:
        return 0.0


def _rank_no(subtext: str) -> int | None:
    m = re.match(r"Rank (\d+)", subtext or "")
    return int(m.group(1)) if m else None


class _Spells:
    """The per-spell rows of one build that the calculator needs."""

    def __init__(self, build_dir: Path):
        self.name = {_i(r["ID"]): r["Name_lang"] for r in read_csv(build_dir / "SpellName.csv")}
        self.subtext = {_i(r["ID"]): r.get("NameSubtext_lang") or "" for r in read_csv(build_dir / "Spell.csv")}
        self.effects: dict[int, list[dict]] = defaultdict(list)
        for r in read_csv(build_dir / "SpellEffect.csv"):
            if _i(r.get("DifficultyID")) == 0:
                self.effects[_i(r["SpellID"])].append(r)
        casts = {_i(r["ID"]): _i(r["Base"]) for r in read_csv(build_dir / "SpellCastTimes.csv")}
        durations = {_i(r["ID"]): _i(r["Duration"]) for r in read_csv(build_dir / "SpellDuration.csv")}
        self.cast, self.duration, self.channeled, self.school = {}, {}, {}, {}
        for r in read_csv(build_dir / "SpellMisc.csv"):
            if _i(r.get("DifficultyID")) == 0:
                sid = _i(r["SpellID"])
                self.cast[sid] = casts.get(_i(r["CastingTimeIndex"]), 0)
                self.duration[sid] = durations.get(_i(r["DurationIndex"]), 0)
                self.channeled[sid] = bool(_i(r["Attributes_1"]) & CHANNELED_ATTR1)
                self.school[sid] = _i(r["SchoolMask"])
        self.levels = {_i(r["SpellID"]): r for r in read_csv(build_dir / "SpellLevels.csv") if _i(r["DifficultyID"]) == 0}
        self.power: dict[int, list[dict]] = defaultdict(list)
        for r in read_csv(build_dir / "SpellPower.csv"):
            self.power[_i(r["SpellID"])].append(r)

    def base_range(self, e: dict) -> tuple[float, float]:
        if "EffectBasePoints" in e and "EffectDieSides" in e:  # Era: BasePoints + 1..DieSides
            bp, die = _f(e["EffectBasePoints"]), _f(e["EffectDieSides"])
            return (bp + 1, bp + die) if die > 0 else (bp, bp)
        mid, var = _f(e["EffectBasePointsF"]), _f(e.get("Variance"))
        return mid * (1 - var / 2), mid * (1 + var / 2)


SCHOOLS = {1: "Physical", 2: "Holy", 4: "Fire", 8: "Nature", 16: "Frost", 32: "Shadow", 64: "Arcane"}


def _school(mask: int) -> str:
    names = [n for bit, n in SCHOOLS.items() if mask & bit]
    return "/".join(names) if names else "—"


def _tick_sources(spell: int, book: _Spells, companions: dict) -> tuple[list[tuple[dict, int]], str | None]:
    """(effect, ticks) pairs to read values from, and how they were found.
    Most spells carry their own effects. Classic channels trigger a missile
    spell every tick (aura 23). Forever's retail-style spells (Hurricane,
    Holy Shock) keep a dummy effect and put the numbers on a companion spell
    with the same name and rank."""
    own = book.effects.get(spell, [])
    duration = book.duration.get(spell, 0)
    out, via = [], None
    for e in own:
        if _i(e["Effect"]) in APPLY_AURA_EFFECTS and _i(e["EffectAura"]) == AURA_PERIODIC_TRIGGER and _i(e["EffectTriggerSpell"]):
            ticks = max(1, duration // _i(e["EffectAuraPeriod"])) if _i(e["EffectAuraPeriod"]) and duration else 1
            out += [(t, ticks) for t in book.effects.get(_i(e["EffectTriggerSpell"]), [])]
            via = "triggered missile"
    if out:
        return out, via
    has_value = any(_i(e["Effect"]) in DIRECT or _i(e["EffectAura"]) in (*AURA_PERIODIC, AURA_ABSORB) for e in own)
    comp = companions.get(spell)
    if has_value and comp:  # e.g. Holy Nova: own damage + a linked heal spell, same cast
        own_roles = {DIRECT[_i(e["Effect"])] for e in own if _i(e["Effect"]) in DIRECT}
        linked = [e for c in comp for e in book.effects.get(c, [])
                  if _i(e["Effect"]) in DIRECT and DIRECT[_i(e["Effect"])] not in own_roles
                  and _f(e["EffectBonusCoefficient"]) > 0]
        if linked:
            return [(e, 1) for e in own] + [(e, 1) for e in linked], "linked spell"
    if not has_value and comp:
        dummy = next((e for e in own if _i(e["EffectAura"]) in (AURA_PERIODIC_DUMMY, AURA_PERIODIC_TRIGGER)), None)
        ticks = max(1, duration // _i(dummy["EffectAuraPeriod"])) if dummy and _i(dummy["EffectAuraPeriod"]) and duration else 1
        for c in comp:
            out += [(e, ticks) for e in book.effects.get(c, []) if _f(e["EffectBonusCoefficient"]) > 0]
        return out, "companion spell"
    return [(e, 1) for e in own], None


def _components(spell: int, book: _Spells, name: str, companions: dict | None = None) -> list[dict]:
    """The spell's scaling effects with their base values and coefficients."""
    out = []
    cast = book.cast.get(spell, 0)
    duration = book.duration.get(spell, 0)
    lv = book.levels.get(spell, {})
    spell_level, base_level = _i(lv.get("SpellLevel")), _i(lv.get("BaseLevel")) or _i(lv.get("SpellLevel"))
    max_level = _i(lv.get("MaxLevel"))
    sources, via = _tick_sources(spell, book, companions or {})
    for e, repeat in sources:
        effect, aura = _i(e["Effect"]), _i(e["EffectAura"])
        if effect in DIRECT and repeat > 1:
            kind, role = "periodic", DIRECT[effect]  # one hit per tick
        elif effect in DIRECT:
            kind, role = "direct", DIRECT[effect]
        elif effect in APPLY_AURA_EFFECTS and aura in AURA_PERIODIC:
            kind, role = "periodic", AURA_PERIODIC[aura]
        elif effect in APPLY_AURA_EFFECTS and aura == AURA_ABSORB:
            kind, role = "absorb", "absorb"
        else:
            continue
        lo, hi = book.base_range(e)
        if lo <= 0 and hi <= 0:
            continue
        ticks = repeat
        if kind == "periodic" and repeat == 1:
            period = _i(e["EffectAuraPeriod"])
            if not period or not duration:
                continue
            ticks = max(1, duration // period)
        per_level = _f(e["EffectRealPointsPerLevel"])
        top = min(REF_LEVEL, max_level) if max_level else REF_LEVEL
        gain = per_level * max(0, top - base_level)
        coef = _f(e["EffectBonusCoefficient"]) * ticks
        source = "client"
        if coef <= 0 and name in SPECIAL and kind in ("direct", "absorb"):
            how, value, note = SPECIAL[name]
            coef = value if how == "flat" else min(max(cast, 1500), 3500) / 3500
            source = "measured"
        out.append({
            "kind": kind, "role": role, "index": _i(e["EffectIndex"]), "ticks": ticks,
            "aoe": _i(e.get("EffectRadiusIndex_0")) > 0,
            "min": round(lo * ticks, 1), "max": round(hi * ticks, 1),
            "min60": round((lo + gain) * ticks, 1), "max60": round((hi + gain) * ticks, 1),
            "scales_with_level": gain > 0,
            "coef": round(coef, 4), "coef_source": source if coef > 0 else "none",
            "special_note": SPECIAL[name][2] if source == "measured" else None,
            "via": via,
        })
    return out


def _formula(comps: list[dict], cast_ms: int, duration_ms: int, channeled: bool) -> None:
    """Attach the classic-formula coefficient (no sub-20 penalty) to each component."""
    cast = (duration_ms if channeled else min(max(cast_ms, 1500), 3500)) / 1000
    direct_share = min(cast, 3.5) / 3.5 if not channeled else min(duration_ms / 1000, 3.5) / 3.5
    dot_share = duration_ms / 1000 / 15
    has_direct = any(c["kind"] == "direct" and c["coef"] > 0 for c in comps)
    has_dot = any(c["kind"] == "periodic" and c["coef"] > 0 for c in comps)
    roles = {c["role"] for c in comps if c["coef"] > 0 and c["kind"] != "absorb"}
    for c in comps:
        if c["kind"] == "absorb":
            c["formula"], c["rule"] = None, "absorb (no formula)"
            continue
        if c["kind"] == "direct":
            value, rule = direct_share, "channeled: duration ÷ 3.5" if channeled else "cast ÷ 3.5"
        elif channeled:
            value, rule = min(duration_ms / 1000, 3.5) / 3.5, "channeled: duration ÷ 3.5"
        else:
            value, rule = dot_share, "duration ÷ 15"
        if has_direct and has_dot:
            total = direct_share + dot_share
            value = value * value / total if total else 0
            rule += ", hybrid split"
        if c["aoe"]:
            value *= 0.5
            rule += ", AoE × 0.5"
        if {"damage", "heal"} <= roles and c["role"] in ("damage", "heal") and c.get("via") != "companion spell":
            value *= 0.5
            rule += ", damage+heal × 0.5"
        c["formula"], c["rule"] = round(value, 4), rule


def sub20_factor(spell_level: int) -> float:
    return 1 - 0.0375 * (20 - spell_level) if spell_level < 20 else 1.0


def _class_spells(forever_dir: Path) -> dict[int, list[int]]:
    """class id → trainer spell ids on its class skill lines (all ranks)."""
    categories = {_i(r["ID"]): r["CategoryID"] for r in read_csv(forever_dir / "SkillLine.csv")}
    skill_class = {}
    for r in read_csv(forever_dir / "SkillRaceClassInfo.csv"):
        mask = _i(r["ClassMask"])
        if mask > 0 and mask & (mask - 1) == 0 and categories.get(_i(r["SkillID"])) == CLASS_SKILL_CATEGORY:
            skill_class.setdefault(_i(r["SkillID"]), mask.bit_length())
    out: dict[int, list[int]] = defaultdict(list)
    seen = set()
    for r in read_csv(forever_dir / "SkillLineAbility.csv"):
        cls = skill_class.get(_i(r["SkillLine"]))
        sid = _i(r["Spell"])
        if cls and r["AcquireMethod"] in TRAINER and (cls, sid) not in seen:
            seen.add((cls, sid))
            out[cls].append(sid)
    return out


def build_downrank(forever_dir: Path, era_dir: Path, base_mana: dict[int, int]) -> tuple[list[tuple], dict, list[str]]:
    """Rows for the downrank_classes table, meta counts, and anchor problems."""
    problems: list[str] = []
    fb, eb = _Spells(forever_dir), _Spells(era_dir)
    class_spells = _class_spells(forever_dir)
    companions = _companions(fb, {sid for ids in class_spells.values() for sid in ids})
    class_names = _class_names(forever_dir)
    stats = defaultdict(int)
    rows = []
    for cls, spell_ids in sorted(class_spells.items()):
        if cls in SKIP_CLASSES:
            continue
        unresolved = set()
        by_name: dict[str, dict[int, int]] = defaultdict(dict)
        for sid in spell_ids:
            mana_rows = [p for p in fb.power.get(sid, []) if p["PowerType"] == POWER_MANA]
            if not mana_rows or sid not in fb.name:
                continue
            name = fb.name[sid]
            if SKIP_NAME.match(name):
                continue
            rank = _rank_no(fb.subtext.get(sid)) or 1
            if rank in by_name[name]:
                stats["duplicate_ranks"] += 1
                continue
            by_name[name][rank] = sid
        spells = []
        for name, ranks in sorted(by_name.items()):
            rank_rows = []
            for rank, sid in sorted(ranks.items()):
                comps = _components(sid, fb, name, companions)
                if not any(c["coef"] > 0 for c in comps):
                    if any((_i(e["Effect"]) == DUMMY_EFFECT and _f(e["EffectBonusCoefficient"]) > 0)
                           or _i(e["EffectAura"]) == AURA_PERIODIC_DUMMY for e in fb.effects.get(sid, [])):
                        unresolved.add(name)  # scales, but the values are server-side (area trigger / script)
                    continue
                cast, duration, channeled = fb.cast.get(sid, 0), fb.duration.get(sid, 0), fb.channeled.get(sid, False)
                _formula(comps, cast, duration, channeled)
                lv = _i(fb.levels.get(sid, {}).get("SpellLevel"))
                for c in comps:
                    if c["coef_source"] == "client" and c["formula"] is not None:
                        stats["formula_checked"] += 1
                        if abs(c["coef"] - c["formula"]) <= MATCH_TOL:
                            stats["formula_match"] += 1
                        elif lv < 20 and abs(c["coef"] - c["formula"] * sub20_factor(lv)) <= MATCH_TOL:
                            stats["formula_match_sub20"] += 1
                    stats[f"coef_{c['coef_source']}"] += 1
                p = [x for x in fb.power[sid] if x["PowerType"] == POWER_MANA][0]
                mana, mana_pct = _i(p["ManaCost"]), _f(p["PowerCostPct"])
                if not mana and mana_pct:
                    mana = round(mana_pct / 100 * base_mana.get(cls, 0))
                row = {"id": sid, "rank": rank, "level": lv, "mana": mana,
                       "mana_pct": mana_pct or None, "cast": cast, "duration": duration,
                       "channeled": channeled, "components": comps}
                row["changes"] = _diff(sid, row, fb, eb)
                rank_rows.append(row)
            if not rank_rows:
                continue
            for entry in _split_either_or(name, rank_rows):
                spells.append(_spell_entry(entry["name"], entry["ranks"], fb, eb, entry.get("role")))
                stats["spells"] += 1
                stats["ranks"] += len(entry["ranks"])
                stats[f"status_{spells[-1]['status']}"] += 1
        name = class_names.get(cls, f"Class {cls}")
        if not spells:
            continue
        _check_anchors(cls, spells, problems)
        stats["unresolved"] += len(unresolved - {s["name"] for s in spells})
        data = {"id": cls, "slug": _slug(name), "name": name, "base_mana_60": base_mana.get(cls), "spells": spells,
                "unresolved": sorted(unresolved - {s["name"] for s in spells})}
        rows.append((_slug(name), cls, name, len(spells), json.dumps(data, separators=(",", ":"))))
    stats["classes"] = len(rows)
    return rows, dict(stats), problems


def _split_either_or(name: str, rank_rows: list[dict]) -> list[dict]:
    """A companion-based spell with separate heal and damage spells (Holy
    Shock: heal a friend *or* damage an enemy) becomes two entries."""
    roles = {c["role"] for r in rank_rows for c in r["components"] if c["coef"] > 0}
    if not ({"heal", "damage"} <= roles and all(c.get("via") == "companion spell"
                                                 for r in rank_rows for c in r["components"])):
        return [{"name": name, "ranks": rank_rows}]
    out = []
    for role in ("heal", "damage"):
        ranks = [{**r, "components": [c for c in r["components"] if c["role"] == role]} for r in rank_rows]
        out.append({"name": f"{name} ({role})", "ranks": ranks, "role": role})
    return out


def _spell_entry(name: str, rank_rows: list[dict], fb: _Spells, eb: _Spells, role: str | None = None) -> dict:
    roles = [c["role"] for r in rank_rows for c in r["components"] if c["coef"] > 0]
    role = role or ("heal" if "heal" in roles else "absorb" if roles and set(roles) == {"absorb"} else "damage")
    in_era = [r for r in rank_rows if r["id"] in eb.name]
    status = "new" if not in_era else "changed" if any(r["changes"] for r in rank_rows) else "unchanged"
    return {"name": name, "role": role, "school": _school(fb.school.get(rank_rows[-1]["id"], 0)),
            "status": status, "hybrid": {"damage", "heal"} <= set(roles) and "(" not in name, "ranks": rank_rows}


def _companions(book: _Spells, trainer: set[int]) -> dict[int, list[int]]:
    """trainer spell id → other spells with the same name, rank and level
    that carry scaling damage/heal effects. Forever's retail-style spells
    (Hurricane, Rain of Fire, Holy Shock…) keep the numbers there; Holy Nova's
    heal half is a separate no-cost spell of the same rank."""
    by_key: dict[tuple, list[int]] = defaultdict(list)

    def key(sid):
        return book.name.get(sid, ""), book.subtext.get(sid, ""), _i(book.levels.get(sid, {}).get("SpellLevel"))
    for sid, effects in book.effects.items():
        if not book.subtext.get(sid, "").startswith("Rank "):
            continue
        if any(_i(e["Effect"]) in DIRECT and _f(e["EffectBonusCoefficient"]) > 0 for e in effects):
            by_key[key(sid)].append(sid)
    out = {}
    for sid in trainer:
        found = [c for c in by_key.get(key(sid), []) if c != sid]
        if found:
            out[sid] = found
    return out


def _diff(sid: int, row: dict, fb: _Spells, eb: _Spells) -> list[str]:
    """What changed for this rank vs the same spell id in Classic Era."""
    if sid not in eb.name:
        return []
    out = []
    era_mana = next((_i(p["ManaCost"]) for p in eb.power.get(sid, []) if p["PowerType"] == POWER_MANA), None)
    if era_mana is not None and era_mana != row["mana"]:
        out.append(f"Mana {era_mana} → {row['mana']}")
    if eb.cast.get(sid, 0) != row["cast"]:
        out.append(f"Cast {eb.cast.get(sid, 0) / 1000:g}s → {row['cast'] / 1000:g}s")
    if eb.duration.get(sid, 0) != row["duration"] and row["duration"]:
        out.append(f"Duration {eb.duration.get(sid, 0) / 1000:g}s → {row['duration'] / 1000:g}s")
    era_effects = {_i(e["EffectIndex"]): e for e in eb.effects.get(sid, [])}
    for c in row["components"]:
        e = era_effects.get(c["index"])
        if e is None:
            continue
        lo, hi = eb.base_range(e)
        era_mid, mid = (lo + hi) / 2 * c["ticks"], (c["min"] + c["max"]) / 2
        if era_mid and abs(mid - era_mid) / era_mid > 0.01:
            out.append(f"Base {c['role']} {era_mid:.0f} → {mid:.0f} (midpoint)")
        era_coef = _f(e["EffectBonusCoefficient"]) * c["ticks"]
        if era_coef > 0 and abs(era_coef - c["coef"]) > 0.005:
            out.append(f"Coefficient {era_coef:.3f} → {c['coef']:.3f}")
    return out


def total_coef(rank: dict, role: str) -> float:
    return sum(c["coef"] for c in rank["components"] if c["role"] == role)


def _check_anchors(cls: int, spells: list[dict], problems: list[str]) -> None:
    for anchor_cls, name, expected in ANCHORS:
        if anchor_cls != cls:
            continue
        spell = next((s for s in spells if s["name"] == name), None)
        if spell is None:
            problems.append(f"anchor {name}: spell missing")
            continue
        got = sorted({round(total_coef(r, spell["role"]), 3) for r in spell["ranks"]})
        if any(abs(g - expected) > 0.005 for g in got):
            problems.append(f"anchor {name}: coefficients {got}, expected {expected:.3f}")
        else:
            log.info("Downrank anchor OK: %s = %.3f on all %d ranks", name, expected, len(spell["ranks"]))


SCHEMA = """
DROP TABLE IF EXISTS downrank_classes;
CREATE TABLE downrank_classes (
    slug TEXT PRIMARY KEY,
    class_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    spell_count INTEGER NOT NULL,
    data_json TEXT NOT NULL
);
"""


def write_tables(conn, rows: list[tuple]) -> None:
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.executemany("INSERT INTO downrank_classes VALUES (?, ?, ?, ?, ?)", rows)
