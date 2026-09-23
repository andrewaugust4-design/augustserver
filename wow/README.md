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

### Base armor (computed, cross-checked the same way)

Armor isn't in the stat array. Forever computes it from four DB2s pulled
with the Forever build (`common/armor.py`):

```
armor  = round(ItemArmorTotal[ilvl][material] × ArmorLocation[invType][material] × ItemArmorQuality[ilvl][quality])
shield = ItemArmorShield[ilvl][quality]        (no location factor)
```

Robes (InventoryType 20) have an all-zero ArmorLocation row and use Chest's.
Only Cloth/Leather/Mail/Plate/Shield subclasses get armor, and cloaks are
Cloth subclass. The generic `Modifier` column is deliberately unused, because
its one checkable case disagreed with Classic. `validate.py` compares every
shared armor item with Classic Era's stored `Resistances_0`. On 2026-09-22
**99.25%** of unchanged items matched (1989/2004), vs a 92% floor, and
93.3% of changed items did. The misses are hand-set armor on rings, necks,
off-hands and totems, plus deprecated/test items. The anchor is Fiery
Slippers = 35 armor. Both the rate floor and the anchor fail the ingest.
The tooltip shows armor first ("35 Armor"), then the stat lines.
`?debug=1` adds `classic_armor`.

## Item effects and set bonuses

Tooltips show **Use: / Equip: / Chance on hit:** lines below the stats, and
an item's **set**: its members, and the threshold bonuses as "(2) Set: …". Both
are rendered to text at ingest by `ingest/effects.py` + `common/spelltext.py`,
which fills `$s1`, `$d`, `$x1`, `${…}.1`, cross-spell `$27648s1` and so on
from the spell tables.

**Readable vs fallback** (as of the 2026-09-23 ingest):
- **Carryover items:** links come from Classic Era's `ItemEffect`, which
  carries `ParentItemID`. Forever's `ItemXItemEffect` is used only as a filter,
  and it agrees with Era wherever both link. What Forever dropped is only
  Equip stat auras (+spell damage, crit, hit, mp5…), which are now real Forever
  item stats and already appear in the stat lines. 1,858 of those Era links
  are skipped so they don't show twice. 458 carryover effect lines render.
- **New Forever items:** only Forever links exist. 189 render; 24 link to
  spells with no readable text, and those show *"effect not yet revealed —
  encrypted in the beta client until looted"*. They fill in automatically when
  the data appears. New items with no link at all say their effects may still
  be encrypted.
- **Text** comes from Forever's spell tables when readable, because Forever
  retuned some procs (the Frost Bolt proc now does 40 to 60, not 20 to 30).
  Otherwise it comes from Era.
- **Anchor:** Thunderfury renders "Chance on hit: Blasts your enemy with
  lightning, dealing 300 Nature damage…". The ingest logs an error if it
  doesn't. (It's 300 in both builds' data, not 35.)
- **Sets** come from **Forever's** `ItemSet`/`ItemSetSpell`, not Era's: all 418
  bonus spells are readable, and Forever changed 18 carryover sets. Imperial
  Plate and Blessed Plate now have five thresholds (2/3/4/5/6). Era text is only
  a fallback. Set names matching placeholder patterns (PH/TBD/test/zz…) are
  tagged "placeholder name" (none in the current build).
- **In the set builder:** equipped pieces count toward their set (by distinct
  item id), and active thresholds light up in the tooltip and on the sheet.
  **Flat** bonuses feed the sheet totals (240 of 418: attributes, armor,
  resistances, AP, health/mana, crit/dodge/parry/block %, defense, block value;
  hit/spell power/mp5 go under "other"). **Proc/effect** bonuses (178) are
  display-only and marked "not in totals".

## Level-gating

With a level set, each slot's list shows only what that character can equip
then:
- **Required level:** the item's required level ≤ the character's level.
- **Armor tier:** the class must have trained the armor type by that level
  (`proficiency.ARMOR_UNLOCK_LEVEL`). Warrior/Paladin plate unlocks at 40 (mail
  below), and Hunter/Shaman mail unlocks at 40 (leather below). This matters:
  some Forever items (e.g. Breastplate of Heroism) have required level 0.
- **Weapons:** gated by required level only, since every usable type trains
  at 1.

Changing level, class or race re-fetches the list and re-checks the equipped
items. Items that no longer qualify are flagged on their slot ("Plate is
trained at level 40.", "Requires level 55."), not removed.

## Set builder (in the Gear Browser)

Click a paperdoll slot to browse it, then click an item to **equip** it (click
it again to take it off). Pick race and level (1–60) next to the class. The
**character sheet** updates live, and **Save & share** mints a permalink
`/wow/gear/set/<id>`.

- **Loadout** = `{class, race, level, slots: {slot: itemId}}`. Slots are
  `common/loadout.py`'s 17 (two rings, two trinkets, and ranged shared with
  relics as in vanilla). Rules:
  - A two-hander clears the off hand, and an off hand clears a two-hander.
  - A unique item can't sit in two slots.
  - One-handers can go in the off hand only for dual-wield classes.
  - Class, race and level problems are *flagged* on the slot, never dropped.
  - Ids missing from the data show as "Item unavailable".
  - `POST /api/sheet` recomputes everything from ids. The frontend enforces
    the same rules at click time.
- **Race/level pickers:** combos come from the client's `CharBaseInfo`,
  including the new ones and both Skyborne races (`ingest/character_data.py`).
  Item lists hide what the character can't equip yet at its level (see
  Level-gating) and flag race-restricted items (`race_ok`).
- **Sheet** (`common/character.py`):
  - Attributes are base + gear (hover for the split), plus health, mana or
    resource, armor and resistances.
  - A combat panel is labelled **"Estimated — unbuffed, no talents"**.
  - Base attributes and health come from the vanilla 1.12 reference in
    `reference/`. Base mana and the crit coefficients come from the client's
    `PlayerExpectedStat`. The formulas come from vmangos.
  - Everything is cross-checked by `ingest/validate_character.py` on every
    ingest (`meta.char_*`). See **`reference/SOURCES.md`** for every source,
    correction and known disagreement.
  - New combos are flagged *derived*, and Skyborne *provisional*.
- **Saved sets** live in **`data/sets.db`** (`WOW_SETS_DB`), separate from
  `wow.db`, which the ingest rebuilds and which nothing in `ingest/` touches.
  - Each save mints a new 8-character id. Rows are never updated, and there's
    no expiry.
  - Only class/race/level/item ids are stored, so old links pick up data fixes.
  - Saves are open to anyone, rate-limited to `WOW_SET_SAVES_PER_HOUR`
    (default 30) per client IP. The IP comes from `CF-Connecting-IP`, since
    nginx has no real_ip config, and is stored hashed.
- The page is also served at `gear/set/<id>`. It sets `<base href>` to
  `…/gear/` so every relative URL keeps working there.

### Still deferred (set builder)

- **Proc-style set bonuses** are shown but not modelled in the totals.
- **Weapon damage / DPS** on the sheet waits on the weapon-damage follow-up
  below. AP is attribute-based only until then.
- **Gear ratings** (hit/crit/dodge/parry/block) aren't converted to %, because
  Forever's rating table is unknown. They're listed under "Other gear bonuses".
- **Shield block value** isn't in the Forever item data, so block value is
  Str-based plus gear "block value" only.

### Not done yet (follow-ups)

- **Weapon damage** is the last computed field not shown yet: min/max damage
  and DPS from the `ItemDamage*` tables × the item's speed (`ItemDelay`). It
  uses the same self-validation against Classic Era's stored
  `MinDamage_0`/`MaxDamage_0`.
- **Hand-set armor on misc items** (e.g. Emerald Circle's 50 armor) isn't
  computed. Forever may express it as bonus-armor stat 50 instead.
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

## Item icons

Icons are **self-hosted**: `GET /api/icon/<itemId>.jpg` serves
`data/icons/<itemId>.jpg` if it's on disk (`Cache-Control: immutable`, and the
`.jpg` suffix lets Cloudflare cache it with no custom rule); on a miss it
asks Blizzard's item media API (`/data/wow/media/item/<id>`) for the icon
URL, downloads it once, and serves it from disk from then on. See
`common/icons.py` and `common/blizzard.py`.

We don't resolve icons from the datamine: in the Forever beta the
`Item → ItemDisplayInfo → icon FileDataID` chain isn't reliably readable yet.
The media API does the id→icon lookup server-side instead.

- **Missing icons** (the API 404s, which is most Forever-only ids during
  beta) get the bundled `app/static/icon-placeholder.svg` with a 1h TTL, and
  a row in `data/icons/missing.db`. That item isn't retried for 24h, so
  coverage fills in over the beta without hitting the API on every view.
  Network/5xx errors are *not* negative-cached.
- **Warm:** `python -m ingest.run --warm-icons` pre-fetches every uncached
  icon after the ingest, highest ilvl first, at 5 items/s. It's resumable,
  because anything on disk or recently missing is skipped.
  `wow-refresh.service` runs with this flag.
- **No keys → no problem:** without `BLIZZARD_CLIENT_ID`/`SECRET` the app
  logs one warning and serves placeholders everywhere.

### `.env` keys

| Key | Default | |
|---|---|---|
| `BLIZZARD_CLIENT_ID`, `BLIZZARD_CLIENT_SECRET` | *(unset → placeholders)* | free client from develop.battle.net |
| `BLIZZARD_REGION` | `us` | API host + namespace suffix |
| `BLIZZARD_ITEM_MEDIA_NAMESPACE` | `static-classic1x-<region>` | from the probe below |
| `WOW_ICON_DIR` | `data/icons` (→ `/opt/wow/data/icons`) | under the unit's `ReadWritePaths` |

### Namespace probe

There's **no Forever namespace**. Blizzard doesn't expose beta/PTR data, and
every guessed Forever-ish name 403s exactly like a made-up one.
`python -m ingest.probe_icons` samples 50 carryover + 50 Forever-only items
per candidate. On 2026-09-22 (seed 1):

| namespace | carryover | new (Forever-only) |
|---|---|---|
| `static-classic1x-us` (Era) | **44/50** | 1/50 |
| `static-classic-us` (progression) | 42/50 | 1/50 |
| `static-us` (retail) | 38/50 | 2/50 |
| `static-classicforever-us`, `static-forever-us`, … | no such ns | no such ns |

The carryover misses are mostly deprecated/test items. So today ~88% of
carryover items and only ~2% of new items get a real icon: roughly
two-thirds of the 7.7k items overall. The ~2,000 new items show the
placeholder until Blizzard publishes a Forever namespace (likely at launch).
When that happens, re-run the probe and set `BLIZZARD_ITEM_MEDIA_NAMESPACE`.
Retail's one extra new-item hit was a genuine match (same name), so a
name-checked retail fallback is a possible later improvement.

## Quest Browser (sub-tool #2, `/wow/quests/`)

A filterable, sortable quest table: zone, required-level range, faction
(Alliance or Horde *including* shared quests, or shared only), a "New to
Forever only" toggle, and search by name or id. It pages 100 rows at a time.
Clicking a row opens the details: objectives, quest giver and turn-in,
rewards with item icons, chain links, and a Wowhead link-out.
The API is `/api/quests`, `/api/quests/zones`, `/api/quests/meta` and
`/api/quest/<id>`, with data built by `ingest/quests.py`.

**Sourcing.** Checked 2026-09-23:
- **Classic quest detail and rewards come from [QuestieDB](https://github.com/Questie/QuestieDB).**
  It's open community data, *downloaded* from GitHub (plain file downloads,
  not scraping) by `ingest/questiedb.py` at each refresh. It's pinned to
  master's current commit, cached in `data/raw/questiedb/<sha>/`, and parsed
  from its Lua source files without executing anything (`ingest/luadata.py`).
  It supplies name, zone, required and quest level, races → faction, classes,
  objectives, quest giver and turn-in (NPC, object or item), chain links, and
  rewards: reward items (from QuestieDB's item → `questRewards` links), XP and
  reputation. The version and commit show in the banner (`/api/quests/meta`).
  The repo has no LICENSE file, so its terms are the Questie project's.
- **QuestieDB has no money reward and doesn't mark choose-one vs guaranteed
  items.** Those two, plus the long description, come from the cmangos 1.12
  `quest_template` in `reference/vanilla_quests.json.gz`. Its reward item sets
  match QuestieDB's on 1,792 of 1,793 quests.
- **The Forever side comes from wago.tools client tables.** The client's quest
  table `QuestV2` is only ID + UniqueBitFlag in both builds; quest details are
  server-side. Forever ids absent from Era's `QuestV2` and from QuestieDB give
  **1,792** "New to Forever" quests. The trackers' 1,795 includes 3 vanilla
  quests that were only newly listed. QuestieDB's own "Forever" flavor is
  Classic data re-projected to Forever's map coordinates, with no new quests,
  so it can't supply these. For 22 new quests the client gives a zone
  (QuestPOIBlob → UiMap), and for a few a quest-line name. Everything else,
  **rewards included, shows "not yet revealed — server-side until seen
  in-world"**. It fills in once QuestieDB or the client catch up, since both
  are re-read every refresh.
- **Wowhead is a human link-out only, never fetched by the tool.** Its
  robots.txt disallows automated agents anyway.
- **XP column.** It shows the *base at-level reward*: the full XP for a
  character near the quest's level, from QuestieDB. Characters well above
  the quest's level get less, and the table's caption says so. The client's
  `QuestXP` DB2 (level × difficulty tier, byte-identical in Forever and Era)
  can't compute XP on its own because each quest's tier is server-side. It's
  used as a cross-check instead: every QuestieDB value must be one of its
  level's tier cells (3,490/3,490 on 2026-09-23). The count is logged each
  ingest and shown in the banner. The 754 detailed quests QuestieDB gives no
  XP for show "—" rather than a guess. The "Obj." column counts objective
  lines (kill/use/collect) as a rough effort guide, with 0 meaning talk-to or
  delivery. Both columns sort.
- **Reward item icons** use the suite's `/api/icon/<id>` cache, and names and
  quality come from the Forever client's ItemSparse.
- **Unidentified carryover ids.** 1,273 carryover ids from Era's client
  aren't in QuestieDB. They're hidden unless "Include unidentified carryover
  ids" is ticked. QuestieDB quests missing from the client list (709, e.g.
  repeatables) are kept, with a note.
- **Dun Morogh examples.** A Visitor to Dun Morogh, Frosthowl and Secure the
  Mountain can't be matched to their ids, since no readable source names new
  quests. None of the 22 new quests with a zone is in Dun Morogh.
- **Anchors checked every ingest:** Kobold Camp Cleanup (Elwynn, Alliance,
  req 1, 170 XP), Your Place In The World (Durotar, Horde), Sharptalon's Claw
  (Ashenvale, Horde, req 20, 2,450 XP), Wanted: "Hogger" (Stormwind Guard
  Leggings among its choice rewards), A Threat Within.

### Quest guides (build, save, share)

Every row has a **+** button, and the details panel has an "Add to guide"
button. Both add the quest to the **guide tray** above the details panel. In
the tray you get an ordered list: drag the ⋮⋮ handle to reorder (▲/▼ also
work on touch and keyboard), add an optional note to each step, and set an
optional title and faction. The draft is kept in this browser's localStorage
until you save. **Save guide** calls `POST /api/guide` with
`{title?, faction?, steps: [{quest_id, note?}]}` and shows the permalink.

- **Guides are immutable, shareable snapshots stored in `data/sets.db`**
  (the `guides` table, next to the gear `sets`). They're not in `wow.db`, so an
  item or quest ingest refresh never touches them. Every save mints a new
  8-character id and rows are never updated. On a guide page, "Edit a copy"
  opens the Quest Browser at `quests/?from=<id>`, and saving that makes a
  *new* link while the original stays as it was.
- **Only quest ids, notes, title and faction are stored.** Zone, level,
  faction, XP, objectives and rewards are read live from `wow.db` on every
  load (`GET /api/guide/<id>`), so old guides get better as the data does.
  An id that's no longer in the data shows as "Quest unavailable" and the rest
  of the guide still works.
- **Saving is open (no password), permanent, and rate-limited per IP.** The
  limit is the same `WOW_SET_SAVES_PER_HOUR` budget as gear sets, shared
  between the two. Limits: 1–200 steps, each quest at most once, notes up to 500
  characters, title up to 120, and every quest id must exist in the data.
- **The guide page** is at `quests/guide/<id>` (`app/static/quests/guide.html`).
  It sets `<base href>` to `…/quests/` like the gear set permalink, so
  relative paths still work. It's an ordered checklist: zone, level, faction,
  XP, objectives, start/turn-in and rewards per step, plus the author's note
  and a total XP. **Check-off progress is per viewer**, saved in localStorage
  under `wow-quest-guide-progress:<id>`, with a "Reset progress" button. If
  storage is blocked, ticking still works for that visit but isn't kept.
- **XP shown is the base at-level reward** (see above), so a guide's XP total
  is an upper bound for a character who's ahead of the quests.
- **Route map** (guide page, above the checklist). It plots each zone's part
  of the guide in the guide's order: quest giver → objective area(s) →
  turn-in for each step, then a dashed line on to the next step. It doesn't
  optimise the route, it only draws the order the author chose.
  - **Markers** are numbered to match the checklist and coloured by role (gold =
    accept, blue = objective, green = turn in). Faint dots show where the
    objective mobs or items spawn. Clicking a marker opens its name and
    outlines the step's card. Each card's "Show on map" button switches to that
    step's zone and zooms to it. Ticked steps fade on the map.
  - **Multi-zone guides** get a zone tab per map, in order of first appearance.
    The first zone is shown by default.
  - **Leaflet 1.9.4** (cdnjs) with `CRS.Simple`, and the zone PNG as an
    `ImageOverlay`. QuestieDB's 0–100 coordinates map to
    `[height × (1 − y/100), width × x/100]`, because the image origin is at the
    top-left while CRS.Simple's y axis grows upward. Code is in
    `app/static/quests/routemap.js`.
  - **Points** come from `ingest/quests.py` (`route_json` column), built from
    QuestieDB spawns. The quest giver and turn-in are NPC or object spawns, or
    where a starting item drops. Objectives use kill-target and object spawns,
    item drop sources (or vendors when an item drops nowhere), and
    `triggerEnd`/`extraObjectives` locations. Each objective's spawns are
    grouped into up to 3 areas (4-unit single-link clusters). If any of those
    are in the quest's own zone or the giver's zone, only those are kept, so
    Tough Wolf Meat points at Elwynn's wolves, not Dun Morogh's. Anchors
    checked every ingest: Kobold Camp Cleanup (McBride 48.9,41.6 → Kobold
    Vermin 49.2,36.3) and Your Place In The World (Kaltunk → Gornek, Durotar).
    On 2026-09-23, 4,030 of the 4,244 detailed quests had at least one point.
  - **Fallbacks.** A step with no locations at all (new Forever quests,
    unavailable ids) says "Not yet mapped". A step missing some parts lists
    what isn't on the map (crafted, profession and dungeon items, for example)
    and still draws giver → turn-in. A zone with no art shows a "Map not
    available for this zone yet" panel instead of the map. If Leaflet fails to
    load, the section says so. The checklist never depends on the map.

**Zone maps are self-hosted.** They're extracted once from the Classic 1.12
(Era) client, not from Forever. QuestieDB's coordinates are Classic ones, and
Forever redrew all 49 zone maps (every tile set differs), so Classic
coordinates only line up with Classic art. The map and route **cover Classic
zones only** until Forever zones are captured. The six new Forever zones
(Mount Hyjal, Darkspear Islands, Zephras Isle, Riverglades, Shen'dralas…) have
no quest coordinates yet and get the placeholder.

```
python -m ingest.maps           # one-time; re-run to pick up a new Era build
```

- `ingest/maps.py` reads `UiMap` (Type 3 = zone) → `UiMapXMapArt` →
  `UiMapArtTile` (a 4×3 grid of 256px BLP tiles), plus `WorldMapOverlay` /
  `WorldMapOverlayTile`: the "explored area" art (towns, lakes, roads)
  composited on top, so the map is fully explored. Each tile is fetched once
  by FileDataID from wago.tools' file endpoint
  (`/api/casc/<fdid>?version=<build>`), throttled, and cached in
  `data/raw/maps/<build>/`. Tiles are decoded with Pillow, stitched and
  cropped to `UiMapArtStyleLayer`'s 1002×668, which is exactly the zone's
  0–1 map space, so no per-zone calibration is needed.
- Output is `data/maps/<UiMap id>.png` (49 zones, about 50 MB) plus
  `data/maps/index.json` (name, AreaTable id, size, build). `deploy.sh`
  excludes `data/`, so run the extract on the server itself. The daily
  ingest doesn't touch these files.
- The app serves them at `/wow/maps/<id>.png` (30-day cache). The
  `location /wow/maps/` block in `deploy/nginx-wow.conf` lets nginx serve
  them straight from disk instead. It's optional.
- Pillow is a new dependency (`requirements.txt`), used only by the extract.
- wago.tools' robots.txt disallows automated agents site-wide, the same as
  for the `/db2/` CSV export the ingest already uses. This is one bulk pull of
  about 1,400 tiles, identified by User-Agent, and cached, not a crawl.

- `app/static/quests/quests.js` holds the rendering helpers (rewards,
  objectives, icons, `api()`) that the browser and the guide page share.

## Refreshing the data

```
python -m ingest.run            # uses cached CSVs if the build hasn't changed
python -m ingest.run --force    # re-download even if cached
```

Run once by hand after first deploy (before starting `wow.service`) to
populate `data/wow.db`, and again after any deploy that changes the DB
schema (the app reads whatever the last ingest wrote). The set-builder
deploy is one of those: it adds item race/unique columns and the
race/class/level tables. In production, `wow-refresh.timer` runs this daily.

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
