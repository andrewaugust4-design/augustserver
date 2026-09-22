"""Client (Forever build) character data for the set builder's sheet:
playable races, the race×class availability matrix, each class's power
type, and per-class/level base mana + crit coefficients.

Everything here is datamined from the Forever build, so it's rebuilt into
wow.db on every refresh. What the client does NOT have (base attributes and
base health) comes from the vanilla reference files in reference/; see
common/character.py and reference/SOURCES.md.

- CharBaseInfo: the race/class combos the character-create screen offers,
  including the new ones (Human Hunter, Orc Mage, Dwarf Shaman, Undead
  Paladin, Gnome Priest) and both Skyborne races.
- ChrRaces: name, faction (`Alliance` column: 0 = Alliance, 1 = Horde), and
  PlayableRaceBit, the bit index into items' AllowableRace mask.
- ChrClassesXPowerTypes: first row per class = its primary power.
- PlayerExpectedStat (Forever-only table): BaseMana, CritPerAgility and
  SpellCritPerIntellect per class and level, as fractions (0.0005 = 0.05%
  per point). These match wowsims' measured Classic values exactly at
  25/40/50/60 (see ingest/validate_character.py).
"""

from __future__ import annotations

import json
from pathlib import Path

from .wago_client import read_csv

CHARACTER_TABLES = ("ChrRaces", "CharBaseInfo", "ChrClassesXPowerTypes", "PlayerExpectedStat")

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "reference"
VANILLA_RACES = {1, 2, 3, 4, 5, 6, 7, 8}

POWER_NAMES = {0: "Mana", 1: "Rage", 3: "Energy"}


def _vanilla_combos() -> set[tuple[int, int]]:
    data = json.loads((REFERENCE_DIR / "vanilla_player_levelstats.json").read_text())
    return {(row[0], row[1]) for row in data["rows"]}


def load_character_data(build_dir: Path) -> dict[str, list[tuple]]:
    """Rows for the races / class_races / class_power / class_level tables."""
    combos = [(int(r["RaceID"]), int(r["ClassID"])) for r in read_csv(build_dir / "CharBaseInfo.csv")]
    playable = {race for race, _ in combos}

    races = []
    for r in read_csv(build_dir / "ChrRaces.csv"):
        race_id = int(r["ID"])
        if race_id not in playable:
            continue
        races.append((
            race_id,
            r["Name_lang"],
            "Horde" if r.get("Alliance") == "1" else "Alliance",
            int(r["PlayableRaceBit"]),
            int(race_id not in VANILLA_RACES),
        ))

    vanilla = _vanilla_combos()
    class_races = [(cls, race, int((race, cls) not in vanilla)) for race, cls in sorted(set(combos))]

    power: dict[int, tuple[int, int]] = {}
    for r in read_csv(build_dir / "ChrClassesXPowerTypes.csv"):
        cls, row_id, ptype = int(r["ClassID"]), int(r["ID"]), int(r["PowerType"])
        if cls not in power or row_id < power[cls][0]:
            power[cls] = (row_id, ptype)
    class_power = [(cls, ptype, POWER_NAMES.get(ptype, f"Power {ptype}")) for cls, (_, ptype) in sorted(power.items())]

    class_level = [
        (int(r["ClassID"]), int(r["Level"]), int(r["BaseMana"]),
         float(r["CritPerAgility"]), float(r["SpellCritPerIntellect"]))
        for r in read_csv(build_dir / "PlayerExpectedStat.csv")
        if int(r.get("ContentSetID") or 0) == 0
    ]
    return {"races": races, "class_races": class_races, "class_power": class_power, "class_level": class_level}


SCHEMA = """
DROP TABLE IF EXISTS races;
CREATE TABLE races (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    faction TEXT NOT NULL,
    playable_bit INTEGER NOT NULL,
    is_new INTEGER NOT NULL
);

DROP TABLE IF EXISTS class_races;
CREATE TABLE class_races (
    class_id INTEGER NOT NULL,
    race_id INTEGER NOT NULL,
    is_new_combo INTEGER NOT NULL,
    PRIMARY KEY (class_id, race_id)
);

DROP TABLE IF EXISTS class_power;
CREATE TABLE class_power (
    class_id INTEGER PRIMARY KEY,
    power_type INTEGER NOT NULL,
    power_name TEXT NOT NULL
);

DROP TABLE IF EXISTS class_level;
CREATE TABLE class_level (
    class_id INTEGER NOT NULL,
    level INTEGER NOT NULL,
    base_mana INTEGER NOT NULL,
    crit_per_agi REAL NOT NULL,
    spell_crit_per_int REAL NOT NULL,
    PRIMARY KEY (class_id, level)
);
"""

INSERTS = {
    "races": "INSERT INTO races VALUES (?, ?, ?, ?, ?)",
    "class_races": "INSERT INTO class_races VALUES (?, ?, ?)",
    "class_power": "INSERT INTO class_power VALUES (?, ?, ?)",
    "class_level": "INSERT INTO class_level VALUES (?, ?, ?, ?, ?)",
}


def write_tables(conn, data: dict[str, list[tuple]]) -> None:
    """Called inside normalize.write_db's transaction."""
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    for table, sql in INSERTS.items():
        conn.executemany(sql, data[table])
