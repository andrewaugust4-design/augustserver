# Character-sheet reference data

The set builder's sheet (`common/character.py`) mixes Forever client data
with vanilla 1.12 reference data. This is where each piece comes from, and
how `ingest/validate_character.py` cross-checks it. The report re-runs on
every ingest; `python -m ingest.validate_character` prints it.

## Files here

| File | What | Source |
|---|---|---|
| `vanilla_player_levelstats.json` | Base Str/Agi/Sta/Int/Spi per race/class/level (vanilla combos only) | [mangoszero/database](https://github.com/mangoszero/database) `World/Setup/FullDB/player_levelstats.sql`. All 2,400 rows are identical to [cmangos/classic-db](https://github.com/cmangos/classic-db) `ClassicDB_1_12_1_z2815`. |
| `vanilla_player_classlevelstats.json` | Base health per class/level (+ the reference base mana, kept only for comparison) | mangoszero `player_classlevelstats.sql`. Identical to cmangos except druid L1 mana. |
| `wowsims_measured.json` | Naked in-game measurements: class base stats at 25/40/50/60, race offsets, base crit/dodge, crit/dodge per Agi, spell crit per Int | [wowsims/sod](https://github.com/wowsims/sod) `sim/core/base_stats.go`. **Validation only**: the sheet never reads it. |

All fetched 2026-09-22.

## What the Forever client has (rebuilt into wow.db on every ingest)

- `CharBaseInfo`: race/class combos, including the new ones.
- `ChrRaces`: faction and `PlayableRaceBit` (items' race mask).
- `ChrClassesXPowerTypes`: each class's resource.
- `PlayerExpectedStat` (a Forever-only table): **BaseMana, CritPerAgility,
  SpellCritPerIntellect** per class/level. These match wowsims'
  measurements at every checked point (36/36 each), so the sheet uses them.

The client has **no** base-attribute or base-health table. `RaceStat` exists
but is all zeros in this build.

## Formulas and constants

- **Health / mana:** first 20 Sta (Int) give 1 HP (mana) each, every point
  after that gives 10 HP (15 mana). From vmangos/cmangos `StatSystem.cpp`, where both agree.
- **Armor:** items + 2 × Agility.
- **Attack power:** vmangos `GetAttackPowerFromStrengthAndAgility`. cmangos
  agrees except for caster-form druids, where it adds level×3. The sheet
  follows vmangos (Str×2 − 20).
- **Base melee crit / base dodge:** vmangos (Nostalrius values). These equal
  wowsims' measured values.
- **Base spell crit:** wowsims' measured values. Both emulators ship an older
  table that their own source marks "MUST BE CHECKED".
- **Dodge per Agi:** crit per Agi, ×2 for Rogue and Hunter. cmangos and
  wowsims agree.
- **Parry / block:** 5% base, ±0.04% per defense point above level × 5. Block
  value is Str/20 − 1 + gear block value (vmangos). The shield's own block
  value isn't in the Forever item data yet.
- **Racials:** Human Spirit and Gnome Expansive Mind (+5%) are already baked
  into the reference rows. The sheet also applies Tauren +5% health, Night Elf
  +1% dodge, and the +10 racial resistances (Dwarf frost, Gnome arcane,
  Night Elf / Tauren nature, Undead shadow).

## Corrections and known disagreements (2026-09-22 report)

- **Base mana:** the reference is +60 for Priest and Mage (and off for Shaman
  at some levels) against both the client and wowsims. The sheet uses the client.
- **Base health:** Priest and Mage are 10 below wowsims at all four measured
  levels, so the sheet adds +10. Shaman at 25/40/50 and Hunter at 50 still
  differ (all classes match at 60). The sheet keeps the reference and the
  report prints these.
- **Attributes:** 155/160 checked rows agree. Orc Warlock Stamina is 1 lower
  than measured at every level, and Gnome Warlock L50 Spirit is off by 1. The
  sheet keeps the reference.

## Provisional

- **New combos** (Human Hunter, Orc Mage, Dwarf Shaman, Undead Paladin, Gnome
  Priest): these are derived as class base + race offset, which reproduces
  every vanilla row that isn't affected by a racial %. The sheet flags them
  "derived".
- **Skyborne** (High Order / Windshaper): race offsets and racials are unknown
  until beta captures exist. The sheet uses the class base with zero offset
  and flags it "provisional".

## Quests (`vanilla_quests.json.gz`)

The vanilla 1.12 `quest_template` from [cmangos/classic-db](https://github.com/cmangos/classic-db),
4,245 quests, fetched 2026-09-22. Rewards are deliberately left out. It's used
only for carryover quests, because the Forever/Era clients carry no quest
details (QuestV2 is id-only).

Compared against [mangoszero/database](https://github.com/mangoszero/database)
`quest_template` (4,243 shared quests):
- **Zones:** agree on 99.1%.
- **Faction:** agrees on 89.8%. Where they differ, cmangos usually restricts
  a quest to one faction and mangoszero says "any race". cmangos is kept.
- **Min level:** differs on 755 quests, mostly by 1–5 levels (cmangos lower).
- **Titles:** differ on 7, punctuation only.
Treat the levels as approximate.
