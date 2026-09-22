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
- Item data comes from the `Item` and `ItemSparse` DB2 tables via
  `https://wago.tools/db2/<Table>/csv?build=<version>` (note: the query
  param is `build`, not `version`). CSVs are cached under `data/raw/<build>/`
  so re-runs on an unchanged build don't re-download.

### The provisional-stats caveat (this is the important part)

Verified directly against both builds' `ItemSparse.csv` headers:

- **Classic Era** stores stats as absolute numbers: `StatModifier_bonusStat_N`
  (a stat type id) paired with `StatModifier_bonusAmount_N` (the real value).
- **The Forever beta build has no `StatModifier_bonusAmount_*` columns at
  all.** It only has `StatModifier_bonusStat_N` (same stat type id) paired
  with `StatPercentEditor_N` — a per-stat weight in basis points against some
  item-level budget curve we don't have. There's no way to turn that into a
  real number without either the actual budget formula or in-game
  confirmation.

So: Classic Era stats are stored and displayed as real numbers. Forever stats
are stored as `{stat_id, weight_bp}` and every consumer (API response,
ingest diff, frontend) marks them `provisional: true` and renders them as a
weight percentage, never as a tooltip-style final number. The Gear Browser's
beta banner repeats this caveat on every page. Don't "fix" this by inventing
a budget curve — if Blizzard/the datamine community publishes one before
launch, implement it for real and drop the provisional flag for computed
values (leave it on for anything still guessed).

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
populate `data/wow.db`. In production, `wow-refresh.timer` runs this daily.

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
