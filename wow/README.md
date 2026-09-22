# WoW: Forever suite

A FastAPI app for augustserver.com's WoW: Forever section — datamined tools
for Blizzard's new Classic+ branch (beta since 2026-09-12, launching
2026-11-04). Ships with one sub-tool, the **Gear Browser**; more WoW tools
slot into the same app/DB later (see "Adding another sub-tool" below).

Deploys to `/opt/wow`, port 8065, proxied at `/wow/` — see the root repo's
`CLAUDE.md` for the site-wide conventions this follows.

## How the data works

There's no HTML scraping of item data. Everything comes from
[wago.tools](https://wago.tools)' public build list and DB2-to-CSV export:

- `GET https://wago.tools/api/builds` lists every product's builds. The
  Forever beta lives under the product slug **`wow_classic_beta`** — verified
  2026-09-22 by cross-referencing the live builds list (the product's version
  scheme jumped from an old `5.5.0.x` Cata-classic-beta cycle to a brand new
  `1.60.1.x` line starting exactly at the BlizzCon announcement date) and by
  reading the build_config's description field off `https://wago.tools/builds`,
  which literally says `WOW-<build>patch1.60.1_ForeverBeta`. `ingest/run.py`
  re-checks that description on every run and logs a warning if a future
  build no longer mentions "Forever" — Blizzard/wago.tools could always
  repoint that product slug at something else later.
- Classic Era baseline is product **`wow_classic_era`**.
- Item data comes from the `Item` and `ItemSparse` DB2 tables, plus
  `RandPropPoints` (Forever build only) for stat budgets, via
  `https://wago.tools/db2/<Table>/csv?build=<version>` (note: the query
  param is `build`, not `version`). CSVs are cached under `data/raw/<build>/`
  so re-runs on an unchanged build don't re-download.

### Stat values (computed, then cross-checked every ingest)

- **Classic Era** stores stats as absolute numbers: `StatModifier_bonusStat_N`
  (a stat type id) paired with `StatModifier_bonusAmount_N`, plus
  resistances in `Resistances_N` (mapped onto the same stat ids Forever uses).
- **Forever** has no `bonusAmount` columns. Each stat is
  `StatModifier_bonusStat_N` + `StatPercentEditor_N`, a weight in basis
  points. The real value is the standard retail itemization formula:

  ```
  value = round(StatPercentEditor / 10000 × RandPropPoints[itemLevel][<Quality>_<slotIndex>])
  ```

  Quality picks the Good/Superior/Epic column family and InventoryType picks
  the column index 0–4 — see `common/stats.py` for both maps. Those maps were
  fitted per InventoryType × quality against Classic Era's stored values,
  not assumed.

**The safety net:** `ingest/validate.py` recomputes every `unchanged` item
(same stat ids/level/quality/slot in both builds) and compares against
Classic Era's real numbers. On 2026-09-22: 99.84% of primary stats
(2536/2540), 100% of resistances (118/118), 99.85% overall; 97.96% even on
`changed` items. It also asserts the Fiery Slippers anchor (+7 Int, +6 Stam,
+4 Spi, +13 fire spell damage). If the primary-stat match rate drops below
95% or the anchor is off, the ingest **exits non-zero and keeps the previous
DB** rather than publishing bad numbers — if that fires after a Blizzard
patch, the budget table or slot/quality mapping changed; re-fit it.
The match rate is stored in `meta` and shown in the Gear Browser banner.

**Stat ids** follow retail's `ItemModType` enum. Every non-primary id was
identified against Classic Era's equip spell for the same item
(`ItemEffect` → `SpellEffect` aura type / school mask; note Classic stores
`EffectBasePoints` as value − 1) or its `Resistances_N`: 51 Fire, 52 Frost,
53 Holy, 54 Shadow, 55 Nature, 56 Arcane resistance (Thunderfury: 51 = +8,
55 = +9); 83–89 are spell damage in school-mask order (85–89 confirmed, 84
holy inferred from the order). Hit/crit/dodge/parry/block come out as
**ratings** (Classic's +1% hit ≈ 10) — we label them as ratings and don't
convert to %. Unknown ids render as "Stat N" and every ingest logs them
(currently 83, 92, 98, 114, 115, 132, 135 — seven items total).

API stats are `{stat_id, name, category, value}`; category is `primary`
(inline "+N Stat"), `resistance`, or `equip` (an "Equip:" line). The raw
weight is kept in the DB as `weight_bp` and returned by
`/api/item/<id>?debug=1`.

### Not done yet (follow-ups)

- **Armor totals and weapon damage** aren't in the stat array. They compute
  from `ItemArmorTotal` / `ItemArmorQuality` / `ArmorLocation` (armor) and
  the `ItemDamage*` tables × item speed (weapon damage) — same approach,
  different tables. Currently not shown.
- **Equip-spell → stat false "changed":** Classic keeps things like spell
  damage/AP/hit as equip spells, Forever as stats, so items like Inferno
  Robe diff as `changed` with an empty Classic side. Folding Classic's
  equip spells into its stat list (the mapping above) would fix that.
- Name the remaining unmapped stat ids as the datamine community does.

### Change detection (new / changed / unchanged)

`ingest/normalize.py` diffs each Forever item against the same item id in
Classic Era: absent from Classic Era → `new`; present in both with a
different name/quality/slot/material/required_level/item_level/stat-id-set →
`changed` (with a per-field before/after in `diff`); otherwise `unchanged`.
Items removed from Forever (present only in Classic Era) aren't surfaced —
the browser lists Forever's item set.

### Class-equip logic

`common/proficiency.py` has the full Classic weapon/armor proficiency matrix
(verified against Warcraft Tavern's per-class guides, not pulled from
memory — see that file's docstring for sources). An item's explicit
`AllowableClass` bitmask wins when present; note some items spell out "no
restriction" as a mask with every class bit set (e.g. `32767`) instead of
`-1` — `can_equip()` treats both forms the same. Otherwise equip-ability
falls back to the proficiency table by armor material rank or weapon
subclass. This is a browsing tool, not an equip simulator: it doesn't model
separate dual-wield proficiency, so a class that can use e.g. daggers will
see dagger results under "Off Hand" even if equipping there actually
requires a talent/passive in-game.

## Refreshing the data

```
python -m ingest.run            # uses cached CSVs if the build hasn't changed
python -m ingest.run --force    # re-download even if cached
```

Run once by hand after first deploy (before starting `wow.service`) to
populate `data/wow.db`, and again after any deploy that changes the DB
schema (the app reads whatever the last ingest wrote). In production, `wow-refresh.timer` runs this daily.

## Local dev

```
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python -m ingest.run
./venv/bin/uvicorn app.main:app --reload --port 8065
```

Then open `http://127.0.0.1:8065/` (suite landing) or `/gear/` (Gear
Browser) directly — no `--root-path` needed for local dev.

## Adding another sub-tool

1. New routes go in `app/main.py` (or split into an `app/api/` module if it
   grows); reuse `common/constants.py` and `common/proficiency.py` — they're
   deliberately not Gear-Browser-specific.
2. New frontend goes under `app/static/<tool>/index.html`, with a matching
   card added to `app/static/index.html`'s tool grid.
3. If it needs new DB2 tables, add the download + parse step to
   `ingest/normalize.py` / `ingest/run.py` and extend the `items` schema (or
   add a new table) rather than duplicating the wago.tools client.
