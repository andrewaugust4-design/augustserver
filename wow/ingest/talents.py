"""Talent Calculator data: Forever's 27 talent trees, per-rank tooltips, and
a diff against Classic Era's trees.

Sources (checked 2026-09-24, Forever 1.60.1.69977 vs Era 1.15.9.69722):

- **Forever's talents are in the modern Trait tables, not `Talent`.** The
  Forever client still ships a `Talent`/`TalentTab` pair, but it's a copy of
  Era's (same 432 rows, same tiers and ranks; only ClassID is zeroed) and
  840 of its 1,357 rank spells no longer exist in Forever's spell tables. The
  live trees are `TraitTree` → `TraitNode` → `TraitNodeEntry` →
  `TraitDefinition` (one spell per talent):
  - **One TraitTree per class**, found through `SkillLineXTraitTree` (class
    skill line → tree) and `SkillRaceClassInfo` (skill line → class mask).
    Each is paid for in TraitCurrency 3820 (`SourcedMax` 51, one point per
    level 10–60). Trees 1081/1083 are unlinked drafts, 1058 a test tree, and
    1187–1189 the Legacy trees (their own 16-point "Legacy Point" currency).
    None of those are read here.
  - **The three specs are three X bands** of one tree, left to right in
    `TalentTab.OrderIndex` order (TalentTab still names them). Nodes sit on a
    600-unit grid: column = (x − band's left edge) / 600, row = (y − top) /
    600, rounded, because a few nodes are nudged by 10 units (e.g. 5030).
  - **Row gates** are `TraitCond.SpentAmountRequired` on each row's node
    group: 5 × row, i.e. the classic "5 points per row" rule, per spec.
  - **Prerequisites** are `TraitEdge`s of type 2/3 (retail's
    Sufficient/RequiredForAvailability). Some are listed both ways (Bestial
    Wrath ↔ Intimidation), so the parent is the node in the earlier row;
    same-row edges (Mind Flay → Improved Mind Flay) keep their direction.
    The client gives no rank requirement, so the parent must be maxed, as in
    classic.
  - **Node flag 8** marks 14 class-tree nodes (e.g. Priest Holy
    Specialization, Warrior Iron Will). Retail calls it TestGridPositioned,
    an editor layout hint; the live Legacy trees carry it too, and these
    nodes sit in empty cells inside the normal row-gate groups, so they're
    treated as real talents. They're counted in the meta (`talents_flag8`).
  - **Per-rank numbers** come from `TraitDefinitionEffectPoints` → `Curve`
    (rank → value for effect N, operation 0 = set). Every multi-rank class
    talent has one, so each rank's tooltip is the spell's description
    rendered with those values (common/spelltext.py's `overrides`).
- **Classic Era side:** its `Talent` table (one spell per rank), matched to a
  Forever talent by spell id (Forever's definition spell is usually Era's
  rank 1), then by name within the class. That gives the badge:
  `new` (no Era match), `reworked` (renamed, different max rank, or any
  rank's tooltip text differs), else `unchanged`. Moving to another row,
  column or spec is recorded in `changes` but doesn't make it "reworked" on
  its own. Era talents with no Forever match are listed as removed.
- **Provisional text**, per rank (`provisional`):
  - `scaling`: the description scales with the character (`$rap`, `$sp`,
    bonus healing…). Those variables are evaluated for a level-60 character
    with no gear bonuses (0 power, ×1 multipliers), e.g. Summon Hawk's 32.
  - `missing`: part of the text references data the client doesn't have
    yet (usually another spell's duration); it's shown as "?".
  `$?cond[A][B]` picks A, colour codes are stripped, and
  `$@spelltooltipN` is inlined before rendering.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from pathlib import Path

from common.spelltext import SpellBook

from .wago_client import read_csv

log = logging.getLogger("wow.ingest")

FOREVER_TABLES = ("TraitTree", "TraitNode", "TraitNodeEntry", "TraitDefinition", "TraitEdge", "TraitCond",
                  "TraitNodeGroupXTraitNode", "TraitNodeGroupXTraitCond", "TraitNodeXTraitNodeEntry",
                  "TraitTreeXTraitCurrency", "TraitCurrency", "TraitDefinitionEffectPoints", "CurvePoint",
                  "SkillLineXTraitTree", "SkillRaceClassInfo", "ChrClasses", "TalentTab")
ERA_TABLES = ("Talent", "TalentTab")

GRID = 600  # TraitNode position units per row/column
BANDS = 3  # specs per class tree
ROWS = 7
POINTS = 51
PER_ROW = 5
MILESTONE_ROWS = (2, 3, 4, 6)  # 1-point talents at 11/16/21/31 points; row 3 (16) is Forever's addition
PREREQ_EDGE_TYPES = {"2", "3"}
FLAG_TEST_GRID = 8


def _i(v) -> int:
    try:
        return int(float(v or 0))
    except ValueError:
        return 0


def _slug(name: str) -> str:
    return re.sub(r"[^a-z]+", "", name.lower())


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip().rstrip(".")


# Leftover spell-description tokens: "$SP", "$ap", "${$AP*0.1}", "$123456s1" …
_UNRESOLVED = re.compile(r"\$\{[^}]*\}(?:\.\d)?|\$[?@]?[A-Za-z_<]\w*>?|\$\d+[A-Za-z]\d?")
# Character-scaling variables inside ${…}: attack/spell power, bonus healing,
# weapon damage → 0 (a level-60 character with no gear bonuses); $<name>
# multipliers → 1; $pl (player level) → 60.
_SCALING_VAR = re.compile(r"\$(?:<\w+>|(spfi|spfr|spn|sps|sph|spa|sp|rap|ap|bh|bc|mws|mw|mhw|ohw|rwb|pl)\b)", re.I)
_CONDITIONAL = re.compile(r"\$\?[^\[\s]*\[([^\]]*)\](?:\[[^\]]*\])?")
_COLOR = re.compile(r"\|[cC][0-9A-Fa-f]{8}|\|[rR]")
_TOOLTIP_REF = re.compile(r"\$@spelltooltip(\d+)")


def _prepare(text: str, book: SpellBook, depth: int = 0) -> tuple[str, bool]:
    """Make a talent description renderable: first branch of `$?cond[A][B]`
    (the "you have the form" wording), no colour codes, `$@spelltooltipN`
    inlined, scaling variables fixed at level 60 with no gear bonus. True if
    any scaling variable was replaced."""
    text = _COLOR.sub("", _CONDITIONAL.sub(lambda m: m.group(1), text))
    if depth < 2:
        text = _TOOLTIP_REF.sub(lambda m: book.render(int(m.group(1)), _prepare(
            book.desc.get(int(m.group(1)), ""), book, depth + 1)[0]) or "", text)
    scaled = False

    def expr(m: re.Match) -> str:
        def var(v: re.Match) -> str:
            nonlocal scaled
            scaled = True
            name = (v.group(1) or "").lower()
            return "60" if name == "pl" else "0" if name else "1"
        return _SCALING_VAR.sub(var, m.group(0))
    text = re.sub(r"\$\{[^}]*\}", expr, text)
    return re.sub(r"\n{3,}", "\n\n", text).strip(), scaled


def _rank_text(book: SpellBook, spell: int, override: str | None = None) -> dict:
    """{text, provisional}: provisional is None, "scaling" (numbers assume a
    level-60 character with no gear bonuses) or "missing" (part of the text
    isn't in the client yet, shown as "?")."""
    raw = override or book.desc.get(spell, "")
    if not raw.strip():
        return {"text": None, "provisional": "missing"}
    prepared, scaled = _prepare(raw, book)
    rendered = book.render(spell, prepared) or ""
    text, missing = _UNRESOLVED.subn("?", rendered)
    return {"text": text, "provisional": "missing" if missing else "scaling" if scaled else None}


def _same_text(forever: str | None, era: str | None) -> bool:
    """Tooltip equality, ignoring case, spacing and "1.0" vs "1", with an
    unresolved "?" on the Era side matching any number."""
    f, e = (re.sub(r"(\d)\.0\b", r"\1", _norm(t)) for t in (forever, era))
    if f == e:
        return True
    return "?" in e and re.fullmatch(re.escape(e).replace(r"\?", r"[\d.]+"), f) is not None


def _curve(points: list[tuple[float, float]], x: float) -> float:
    pts = sorted(points)
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def _class_names(build_dir: Path) -> dict[int, str]:
    # ChrClasses has no ID column in these exports; rows are in class-id order
    # but skip unused ids (6 DK, 10 Monk), so name them from Filename instead.
    known = {"WARRIOR": 1, "PALADIN": 2, "HUNTER": 3, "ROGUE": 4, "PRIEST": 5, "SHAMAN": 7, "MAGE": 8,
             "WARLOCK": 9, "DRUID": 11}
    out = {}
    for r in read_csv(build_dir / "ChrClasses.csv"):
        cid = _i(r.get("ID")) or known.get(r.get("Filename", ""))
        if cid:
            out[cid] = r["Name_lang"]
    return out


def _tabs(build_dir: Path) -> dict[int, list[dict]]:
    """class id → its three TalentTabs in OrderIndex order."""
    by_class: dict[int, list[dict]] = defaultdict(list)
    for r in read_csv(build_dir / "TalentTab.csv"):
        mask = _i(r["ClassMask"])
        if mask and mask & (mask - 1) == 0:
            by_class[mask.bit_length()].append(r)
    return {c: sorted(rows, key=lambda r: _i(r["OrderIndex"])) for c, rows in by_class.items()}


def _icons(build_dir: Path) -> dict[int, int]:
    """spell id → icon FileDataID (SpellMisc, base difficulty)."""
    out = {}
    for r in read_csv(build_dir / "SpellMisc.csv"):
        if _i(r.get("DifficultyID")) == 0 and _i(r.get("SpellIconFileDataID")):
            out.setdefault(_i(r["SpellID"]), _i(r["SpellIconFileDataID"]))
    return out


def _era_talents(era_dir: Path, book: SpellBook) -> dict[int, list[dict]]:
    """class id → Era talents with per-rank rendered text."""
    tabs = _tabs(era_dir)
    tab_pos = {_i(t["ID"]): (cid, i, t["Name_lang"]) for cid, ts in tabs.items() for i, t in enumerate(ts)}
    out: dict[int, list[dict]] = defaultdict(list)
    for r in read_csv(era_dir / "Talent.csv"):
        spells = [_i(r[f"SpellRank_{i}"]) for i in range(9) if _i(r[f"SpellRank_{i}"])]
        if not spells or _i(r["TabID"]) not in tab_pos:
            continue
        cid, tab, tab_name = tab_pos[_i(r["TabID"])]
        ranks = [_rank_text(book, s) for s in spells]
        out[cid].append({
            "id": _i(r["ID"]), "name": book.name.get(spells[0], f"Talent {r['ID']}"), "spells": spells,
            "tab": tab, "tab_name": tab_name, "row": _i(r["TierID"]), "col": _i(r["ColumnIndex"]),
            "max_rank": len(spells), "ranks": ranks,
        })
    return out


def _class_trees(forever_dir: Path) -> dict[int, int]:
    """class id → TraitTree id, via the class skill line."""
    skill_class = {}
    for r in read_csv(forever_dir / "SkillRaceClassInfo.csv"):
        mask = _i(r["ClassMask"])
        if mask > 0 and mask & (mask - 1) == 0:
            skill_class.setdefault(_i(r["SkillID"]), mask.bit_length())
    return {skill_class[_i(r["SkillLineID"])]: _i(r["TraitTreeID"])
            for r in read_csv(forever_dir / "SkillLineXTraitTree.csv") if _i(r["SkillLineID"]) in skill_class}


def build_talents(forever_dir: Path, era_dir: Path) -> tuple[list[tuple], dict, list[str]]:
    """Rows for the talent_classes table, meta counts, and validation problems."""
    problems: list[str] = []
    fbook, ebook = SpellBook(forever_dir), SpellBook(era_dir)
    ficons, eicons = _icons(forever_dir), _icons(era_dir)
    class_names = _class_names(forever_dir)
    tabs = _tabs(forever_dir)
    era = _era_talents(era_dir, ebook)

    nodes = {_i(r["ID"]): r for r in read_csv(forever_dir / "TraitNode.csv")}
    entries = {_i(r["ID"]): r for r in read_csv(forever_dir / "TraitNodeEntry.csv")}
    defs = {_i(r["ID"]): r for r in read_csv(forever_dir / "TraitDefinition.csv")}
    node_entries: dict[int, list[int]] = defaultdict(list)
    for r in sorted(read_csv(forever_dir / "TraitNodeXTraitNodeEntry.csv"), key=lambda r: _i(r["_Index"])):
        node_entries[_i(r["TraitNodeID"])].append(_i(r["TraitNodeEntryID"]))
    curves: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for r in read_csv(forever_dir / "CurvePoint.csv"):
        curves[_i(r["CurveID"])].append((float(r["Pos_0"]), float(r["Pos_1"])))
    effect_points: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for r in read_csv(forever_dir / "TraitDefinitionEffectPoints.csv"):
        if r["OperationType"] == "0":
            effect_points[_i(r["TraitDefinitionID"])].append((_i(r["EffectIndex"]), _i(r["CurveID"])))
    edges = [(_i(r["LeftTraitNodeID"]), _i(r["RightTraitNodeID"]))
             for r in read_csv(forever_dir / "TraitEdge.csv") if r["Type"] in PREREQ_EDGE_TYPES]
    currency_max = {_i(r["ID"]): _i(r["SourcedMax"]) for r in read_csv(forever_dir / "TraitCurrency.csv")}
    tree_currency = {_i(r["TraitTreeID"]): _i(r["TraitCurrencyID"])
                     for r in read_csv(forever_dir / "TraitTreeXTraitCurrency.csv")}
    group_gate = {}
    for r in read_csv(forever_dir / "TraitCond.csv"):
        if _i(r["SpentAmountRequired"]):
            group_gate[_i(r["ID"])] = _i(r["SpentAmountRequired"])
    node_gate: dict[int, int] = {}
    group_nodes: dict[int, list[int]] = defaultdict(list)
    for r in read_csv(forever_dir / "TraitNodeGroupXTraitNode.csv"):
        group_nodes[_i(r["TraitNodeGroupID"])].append(_i(r["TraitNodeID"]))
    for r in read_csv(forever_dir / "TraitNodeGroupXTraitCond.csv"):
        gate = group_gate.get(_i(r["TraitCondID"]))
        if gate:
            for n in group_nodes[_i(r["TraitNodeGroupID"])]:
                node_gate[n] = max(node_gate.get(n, 0), gate)

    rows: list[tuple] = []
    totals = defaultdict(int)
    for class_id, tree_id in sorted(_class_trees(forever_dir).items()):
        name = class_names.get(class_id, f"Class {class_id}")
        if currency_max.get(tree_currency.get(tree_id)) != POINTS:
            problems.append(f"{name}: tree {tree_id} currency max is {currency_max.get(tree_currency.get(tree_id))}, not {POINTS}")
        tree_nodes = [dict(n) for n in nodes.values()
                      if _i(n["TraitTreeID"]) == tree_id and node_entries.get(_i(n["ID"]))]
        tree_nodes = _repair_positions(name, tree_nodes, entries, defs, node_entries, node_gate, problems)
        # Split into the three spec bands at the two widest gaps in X.
        xs = sorted({_i(n["PosX"]) for n in tree_nodes})
        cuts = sorted(sorted(range(1, len(xs)), key=lambda i: xs[i] - xs[i - 1])[-(BANDS - 1):])
        lefts = [xs[0]] + [xs[i] for i in cuts]
        top = min(_i(n["PosY"]) for n in tree_nodes)

        talents: list[dict] = []
        by_node: dict[int, dict] = {}
        for n in tree_nodes:
            nid, x, y = _i(n["ID"]), _i(n["PosX"]), _i(n["PosY"])
            tab = max(i for i, left in enumerate(lefts) if x >= left)
            entry = entries[node_entries[nid][0]]
            if len(node_entries[nid]) > 1:
                problems.append(f"{name}: node {nid} has {len(node_entries[nid])} entries (choice node?); using the first")
            d = defs[_i(entry["TraitDefinitionID"])]
            spell = _i(d["SpellID"])
            max_rank = _i(entry["MaxRanks"])
            ranks = []
            for rank in range(1, max_rank + 1):
                fbook.overrides = {(spell, eff): _curve(curves[cid], rank)
                                   for eff, cid in effect_points.get(_i(d["ID"]), []) if curves.get(cid)}
                ranks.append(_rank_text(fbook, spell, d["OverrideDescription_lang"]))
            fbook.overrides = {}
            t = {
                "id": nid, "spell_id": spell,
                "name": d["OverrideName_lang"] or fbook.name.get(spell) or ebook.name.get(spell) or f"Talent {nid}",
                "tab": tab, "row": round((y - top) / GRID), "col": round((x - lefts[tab]) / GRID),
                "max_rank": max_rank, "icon": _i(d["OverrideIcon"]) or ficons.get(spell) or eicons.get(spell) or 0,
                "ranks": ranks, "prereqs": [], "gate": node_gate.get(nid, 0),
            }
            if _i(n["Flags"]) & FLAG_TEST_GRID:
                totals["flag8"] += 1
            talents.append(t)
            by_node[nid] = t

        # Prerequisites: parent is the earlier-row end of the edge.
        for left, right in edges:
            a, b = by_node.get(left), by_node.get(right)
            if not a or not b:
                continue
            if a["tab"] != b["tab"]:
                problems.append(f"{name}: prereq edge {a['name']} → {b['name']} crosses specs; ignored")
                continue
            if (a["row"], a["tab"]) > (b["row"], b["tab"]):
                continue  # the reverse half of a two-way edge
            if a["row"] == b["row"] and (right, left) in edges and a["col"] > b["col"]:
                continue
            if a["id"] not in b["prereqs"]:
                b["prereqs"].append(a["id"])

        # Match each talent to Era's version of it.
        era_list = era.get(class_id, [])
        by_spell = {s: e for e in era_list for s in e["spells"]}
        by_name = {_norm(e["name"]): e for e in era_list}
        matched = set()
        for t in talents:
            e = by_spell.get(t["spell_id"]) or by_name.get(_norm(t["name"]))
            if e is None or e["id"] in matched:
                t["status"], t["changes"], t["classic"] = "new", [], None
                continue
            matched.add(e["id"])
            changes = []
            if _norm(e["name"]) != _norm(t["name"]):
                changes.append(f"Renamed from {e['name']}")
            if e["max_rank"] != t["max_rank"]:
                changes.append(f"Max rank {e['max_rank']} → {t['max_rank']}")
            if not all(_same_text(f["text"], c["text"]) for f, c in zip(t["ranks"], e["ranks"])):
                changes.append("Tooltip changed")
            reworked = bool(changes)
            if e["tab"] != t["tab"]:
                changes.append(f"Moved from {e['tab_name']}")
            elif (e["row"], e["col"]) != (t["row"], t["col"]):
                changes.append(f"Moved from row {e['row'] + 1}, column {e['col'] + 1}")
            t["status"] = "reworked" if reworked else "unchanged"
            t["changes"] = changes
            t["classic"] = {"name": e["name"], "max_rank": e["max_rank"], "tab_name": e["tab_name"],
                            "row": e["row"], "col": e["col"], "ranks": e["ranks"]}
        removed = [{"name": e["name"], "tab_name": e["tab_name"], "max_rank": e["max_rank"],
                    "text": e["ranks"][-1]["text"]} for e in era_list if e["id"] not in matched]

        talents.sort(key=lambda t: (t["tab"], t["row"], t["col"]))
        class_tabs = tabs.get(class_id, [])
        trees = []
        for i in range(BANDS):
            members = [t for t in talents if t["tab"] == i]
            tab_row = class_tabs[i] if i < len(class_tabs) else {}
            trees.append({
                "index": i, "name": tab_row.get("Name_lang") or f"Tree {i + 1}",
                "icon": _i(tab_row.get("SpellIconID")), "background": tab_row.get("BackgroundFile") or "",
                "max_points": sum(t["max_rank"] for t in members),
            })
            _check_tree(name, trees[-1]["name"], members, problems)
        for t in talents:
            totals[t["status"]] += 1
            totals["count"] += 1
            for kind in {r["provisional"] for r in t["ranks"]} - {None}:
                totals[f"provisional_{kind}"] += 1
        totals["removed"] += len(removed)
        data = {"id": class_id, "slug": _slug(name), "name": name, "tree_id": tree_id,
                "points": POINTS, "per_row": PER_ROW, "rows": ROWS, "trees": trees,
                "talents": talents, "removed": removed}
        rows.append((_slug(name), class_id, name, len(talents), json.dumps(data, separators=(",", ":"))))
    totals["classes"] = len(rows)
    totals["trees"] = sum(len(json.loads(r[4])["trees"]) for r in rows)
    return rows, dict(totals), problems


def _repair_positions(cls: str, tree_nodes: list[dict], entries: dict, defs: dict, node_entries: dict,
                      node_gate: dict, problems: list[str]) -> list[dict]:
    """Fix the client's extra-digit position typos (Improved Serpent Sting
    sits at y=39300 for 3930), and drop an off-grid node when the same spell
    also has a properly placed node (Lightning Reflexes and Holy
    Specialization each have a stray copy plus a flag-8 re-placement)."""
    top = min(_i(n["PosY"]) for n in tree_nodes)
    max_x = sorted(_i(n["PosX"]) for n in tree_nodes)[len(tree_nodes) * 9 // 10]  # ignore the outliers
    max_y = top + (ROWS - 1) * GRID

    def spell(n):
        return _i(defs[_i(entries[node_entries[_i(n["ID"])][0]]["TraitDefinitionID"])]["SpellID"])

    def off_grid(n):
        return _i(n["PosX"]) > max_x + GRID or _i(n["PosY"]) > max_y + GRID // 2

    by_spell = defaultdict(list)
    for n in tree_nodes:
        by_spell[spell(n)].append(n)
    kept = []
    for n in tree_nodes:
        if off_grid(n) and any(not off_grid(m) for m in by_spell[spell(n)] if m is not n):
            log.info("Talents: %s: dropped stray duplicate node %s at (%s, %s)", cls, n["ID"], n["PosX"], n["PosY"])
            continue
        if off_grid(n):
            x, y = _i(n["PosX"]), _i(n["PosY"])
            while x > max_x + GRID:
                x //= 10
            while y > max_y + GRID // 2:
                y //= 10
            gate = node_gate.get(_i(n["ID"]))
            row = round((y - top) / GRID)
            if gate and gate != PER_ROW * row:
                problems.append(f"{cls}: node {n['ID']} at ({n['PosX']}, {n['PosY']}) can't be placed; dropped")
                continue
            log.info("Talents: %s: node %s moved from (%s, %s) to (%d, %d)", cls, n["ID"], n["PosX"], n["PosY"], x, y)
            n["PosX"], n["PosY"] = str(x), str(y)
        kept.append(n)
    return kept


def _check_tree(cls: str, tree: str, members: list[dict], problems: list[str]) -> None:
    where = f"{cls} {tree}"
    cells = defaultdict(list)
    for t in members:
        cells[(t["row"], t["col"])].append(t["name"])
        if not (0 <= t["row"] < ROWS and 0 <= t["col"] < 4):
            problems.append(f"{where}: {t['name']} is off the 7×4 grid at row {t['row']}, col {t['col']}")
        if t["gate"] and t["gate"] != PER_ROW * t["row"]:
            problems.append(f"{where}: {t['name']} row {t['row']} is gated at {t['gate']} points, not {PER_ROW * t['row']}")
        if not 1 <= t["max_rank"] <= 5 or t["max_rank"] == 4:
            problems.append(f"{where}: {t['name']} has max rank {t['max_rank']}")
        if not t["icon"]:
            problems.append(f"{where}: {t['name']} has no icon")
    for cell, names in cells.items():
        if len(names) > 1:
            problems.append(f"{where}: {names} share row {cell[0]}, col {cell[1]}")
    for row in MILESTONE_ROWS:
        if not any(t["row"] == row and t["max_rank"] == 1 for t in members):
            problems.append(f"{where}: no 1-point milestone talent in row {row + 1} ({PER_ROW * row + 1} points)")
    if {t["row"] for t in members} != set(range(ROWS)):
        problems.append(f"{where}: rows used are {sorted({t['row'] for t in members})}")


SCHEMA = """
DROP TABLE IF EXISTS talent_classes;
CREATE TABLE talent_classes (
    slug TEXT PRIMARY KEY,
    class_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    talent_count INTEGER NOT NULL,
    data_json TEXT NOT NULL
);
"""


def write_tables(conn, rows: list[tuple]) -> None:
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.executemany("INSERT INTO talent_classes VALUES (?, ?, ?, ?, ?)", rows)
