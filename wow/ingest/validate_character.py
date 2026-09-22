"""Cross-checks for the set builder's character sheet (common/character.py).

    python -m ingest.validate_character      # print the full report

Also runs at the end of every ingest (logged, summary stored in meta). It
never blocks the refresh — the sheet is labelled estimated — but every
disagreement between sources is printed rather than silently shipped.

What's compared (see reference/SOURCES.md):
1. Base attributes: vanilla reference (mangos) vs wowsims' naked in-game
   measurements at 25/40/50/60, with the Human/Gnome +5% racials explained.
2. Base health: reference (+ the Priest/Mage correction) vs wowsims.
3. Base mana: Forever client vs reference vs wowsims.
4. Crit per Agility / spell crit per Intellect: client vs wowsims.
5. Dodge per Agility as the sheet derives it vs wowsims.
6. Level-60 naked sheets for a spread of race/class points vs values
   rebuilt independently from wowsims' measured inputs.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from common import character as ch
from common.constants import CLASSES

log = logging.getLogger("wow.ingest")

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "reference"
ATTR_KEYS = ("Strength", "Agility", "Stamina", "Intellect", "Spirit")
CLASS_NAME = {c["class_id"]: c["name"] for c in CLASSES}
SLUG = {c["class_id"]: c["slug"] for c in CLASSES}
RACE_NAME = {1: "Human", 2: "Orc", 3: "Dwarf", 4: "Night Elf", 5: "Undead", 6: "Tauren", 7: "Gnome", 8: "Troll"}

# Level-60 unbuffed spot checks: (race, class)
SPOT_CHECKS = [(1, 1), (6, 1), (4, 3), (8, 4), (3, 2), (5, 5), (8, 8), (2, 9), (6, 11), (8, 7)]


def _ws() -> dict:
    data = json.loads((REFERENCE_DIR / "wowsims_measured.json").read_text())
    conv = lambda d: {int(k): ({int(l): v for l, v in lv.items()} if isinstance(lv, dict) else lv) for k, lv in d.items()}
    return {
        "base": {int(c): {int(l): v for l, v in lv.items()} for c, lv in data["class_base_stats"].items()},
        "race": {int(r): v for r, v in data["race_offsets"].items()},
        "crit": conv(data["crit_per_agi_pct"]),
        "spell": conv(data["spell_crit_per_int_pct"]),
        "dodge": conv(data["dodge_per_agi_pct"]),
        "base_crit": {int(c): v for c, v in data["class_base_crit"].items()},
    }


def run(conn: sqlite3.Connection, emit=print) -> dict[str, str]:
    conn.row_factory = sqlite3.Row
    ws = _ws()
    levelstats, basehp = ch._reference()
    ref_mana = {(c, l): m for c, l, _, m in json.loads(
        (REFERENCE_DIR / "vanilla_player_classlevelstats.json").read_text())["rows"]}
    client = {(r[0], r[1]): tuple(r) for r in conn.execute(
        "SELECT class_id, level, base_mana, crit_per_agi, spell_crit_per_int FROM class_level")}
    summary: dict[str, str] = {}

    # 1. base attributes
    exact = racial = other = 0
    unexplained = []
    for cid, levels in ws["base"].items():
        for lvl, st in levels.items():
            for race in RACE_NAME:
                row = levelstats.get((race, cid, lvl))
                if row is None:
                    continue
                off = ws["race"].get(race, {})
                expect = [st[k] + off.get(k, 0) for k in ATTR_KEYS]
                if list(row) == [int(e) for e in expect]:
                    exact += 1
                    continue
                pct = ch.RACE_PCT.get(race)
                idx = ch.ATTRS.index(pct[0]) if pct else None
                diffs = [i for i in range(5) if row[i] != expect[i]]
                if pct and diffs == [idx] and abs(row[idx] - expect[idx] * pct[1]) <= 1.5:  # rounding of the 5%
                    racial += 1
                else:
                    other += 1
                    unexplained.append(f"{RACE_NAME[race]} {CLASS_NAME[cid]} L{lvl}: reference {list(row)} vs measured {[int(e) for e in expect]}")
    emit(f"[attributes] reference vs wowsims measured: {exact} exact, {racial} differ only by the +5% racial, {other} unexplained")
    for u in unexplained:
        emit(f"    ! {u}")
    summary["char_attr_check"] = f"{exact + racial}/{exact + racial + other}"

    # 2. base health
    hp_ok, hp_bad = 0, []
    for cid, levels in ws["base"].items():
        for lvl, st in levels.items():
            got = ch.base_health(cid, lvl)
            if got == int(st["Health"]):
                hp_ok += 1
            else:
                hp_bad.append(f"{CLASS_NAME[cid]} L{lvl}: sheet {got} vs measured {int(st['Health'])} (raw reference {basehp[(cid, lvl)]})")
    emit(f"[base health] sheet vs wowsims measured: {hp_ok}/{hp_ok + len(hp_bad)} match")
    for b in hp_bad:
        emit(f"    ! {b}")
    summary["char_hp_check"] = f"{hp_ok}/{hp_ok + len(hp_bad)}"

    # 3. base mana
    mana_rows = [(c, l) for (c, l) in client if l <= 60 and client[(c, l)][2] > 0]
    ref_agree = sum(client[k][2] == ref_mana.get(k) for k in mana_rows)
    ws_pts = [(c, l) for c, lv in ws["base"].items() for l in lv if lv[l]["Mana"] > 0]
    ws_agree = sum(client.get((c, l), (0, 0, None))[2] == int(ws["base"][c][l]["Mana"]) for c, l in ws_pts)
    emit(f"[base mana] client vs wowsims measured: {ws_agree}/{len(ws_pts)} match; "
         f"client vs vanilla reference: {ref_agree}/{len(mana_rows)} (sheet uses the client)")
    for cid in sorted({c for c, _ in mana_rows}):
        diffs = sorted({client[(cid, l)][2] - ref_mana[(cid, l)] for l in range(1, 61) if (cid, l) in client and (cid, l) in ref_mana})
        if diffs != [0]:
            emit(f"    ! {CLASS_NAME[cid]}: client − reference ∈ {diffs[:6]}{'…' if len(diffs) > 6 else ''}")
    summary["char_mana_check"] = f"{ws_agree}/{len(ws_pts)}"

    # 4/5. coefficients
    def coeff(name, ws_table, client_value):
        ok = bad = 0
        for cid, levels in ws_table.items():
            for lvl, measured in levels.items():
                if (cid, lvl) not in client:
                    continue
                got = client_value(cid, lvl)
                if abs(got - measured) <= 0.00015 + measured * 0.005:
                    ok += 1
                else:
                    bad += 1
                    emit(f"    ! {name} {CLASS_NAME[cid]} L{lvl}: sheet {got:.4f} vs measured {measured:.4f}")
        emit(f"[{name}] sheet vs wowsims measured: {ok}/{ok + bad} match")
        summary[f"char_{name.replace(' ', '_')}_check"] = f"{ok}/{ok + bad}"

    coeff("crit per agi", ws["crit"], lambda c, l: client[(c, l)][3] * 100)
    coeff("spell crit per int", ws["spell"], lambda c, l: client[(c, l)][4] * 100)
    coeff("dodge per agi", ws["dodge"], lambda c, l: client[(c, l)][3] * 100 * ch.DODGE_AGI_MULT.get(c, 1.0))
    base_ok = all(ch.BASE_MELEE_CRIT.get(c, 0) == v.get("MeleeCrit", 0) and ch.BASE_SPELL_CRIT.get(c, 0) == v.get("SpellCrit", 0)
                  and ch.BASE_DODGE.get(c, 0) == v.get("Dodge", 0) for c, v in ws["base_crit"].items())
    emit(f"[base crit/dodge] sheet constants vs wowsims measured: {'all match' if base_ok else 'MISMATCH'}")

    # 6. level-60 naked spot checks, rebuilt from wowsims inputs
    races = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM races")} if _has(conn, "races") else {}
    power = {r[0]: r[1] for r in conn.execute("SELECT class_id, power_name FROM class_power")} if _has(conn, "class_power") else {}
    spot_ok = 0
    for race, cid in SPOT_CHECKS:
        if race not in races or (cid, 60) not in client:
            continue
        cl = client[(cid, 60)]
        sheet = ch.compute_sheet(class_slug=SLUG[cid], race=races[race], level=60, is_new_combo=False,
                                 class_level={"base_mana": cl[2], "crit_per_agi": cl[3], "spell_crit_per_int": cl[4]},
                                 power_name=power.get(cid, "Mana"), items=[])
        st = ws["base"][cid][60]
        off = ws["race"].get(race, {})
        m = {k: st[k] + off.get(k, 0) for k in ATTR_KEYS}
        if race in ch.RACE_PCT:
            key = ch.RACE_PCT[race][0].capitalize()
            m[key] = round(m[key] * ch.RACE_PCT[race][1])
        hp = st["Health"] + ch.health_from_stamina(m["Stamina"])
        if race == 6:
            hp *= ch.TAUREN_HEALTH_MULT
        expect = {
            "health": round(hp),
            "mana": round(st["Mana"] + ch.mana_from_intellect(m["Intellect"])) if st["Mana"] else None,
            "melee_crit": round(ws["base_crit"][cid]["MeleeCrit"] + m["Agility"] * ws["crit"][cid][60], 2),
        }
        got = {"health": sheet["health"], "mana": sheet["mana"], "melee_crit": sheet["combat"]["melee_crit"]}
        close = got["health"] == expect["health"] and got["mana"] == expect["mana"] and abs(got["melee_crit"] - expect["melee_crit"]) <= 0.05
        spot_ok += close
        emit(f"[L60 naked] {RACE_NAME[race]} {CLASS_NAME[cid]}: sheet HP {got['health']} / mana {got['mana']} / crit {got['melee_crit']}%"
             + ("" if close else f"   ! measured-input rebuild HP {expect['health']} / mana {expect['mana']} / crit {expect['melee_crit']}%"))
    summary["char_spot_check"] = f"{spot_ok}/{len(SPOT_CHECKS)}"
    return summary


def _has(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def main() -> None:
    from .run import DB_PATH

    conn = sqlite3.connect(DB_PATH)
    summary = run(conn)
    print("\nsummary:", summary)


if __name__ == "__main__":
    main()
