"""WoW: Forever suite — shared data API + Gear Browser (sub-tool #1).

Runs behind a reverse proxy at /wow (see deploy/ for nginx + systemd). All
item data comes from a SQLite DB built by `python -m ingest.run`, which
diffs the Forever beta against Classic Era via the wago.tools DB2 export —
see common/stats.py for how Forever stat values are computed and
ingest/normalize.py for how "changed" is derived. This app never talks to wago.tools itself; it only reads the DB.
"""

import json
import re
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from common import icons
from common.character import compute_sheet
from common.constants import CLASS_BY_SLUG, CLASSES, SLOT_BY_SLUG, SLOTS
from common.loadout import INVTYPE_ONE_HAND, INVTYPE_TWO_HAND, LOADOUT_SLOTS, check, is_shield, race_allowed
from common.proficiency import DUAL_WIELD_CLASSES, armor_trained_by, can_equip

from . import db, sets

app = FastAPI(title="WoW: Forever", docs_url=None, redoc_url=None)

STATIC_DIR = Path(__file__).parent / "static"
PLACEHOLDER_ICON = STATIC_DIR / "icon-placeholder.svg"


def _public_stats(stats: list[dict], debug: bool) -> list[dict]:
    """Drop the raw StatPercentEditor weight unless ?debug=1."""
    if debug:
        return stats
    return [{k: v for k, v in s.items() if k != "weight_bp"} for s in stats]


def _row_to_summary(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "quality": row["quality"],
        "slot": row["slot"],
        "material": row["material"],
        "item_level": row["item_level"],
        "required_level": row["required_level"],
        "change_status": row["change_status"],
        "inventory_type": row["inventory_type"],
        "two_hand": row["inventory_type"] == INVTYPE_TWO_HAND,
        "unique": bool(row["unique_equipped"]) if "unique_equipped" in row.keys() else False,
    }


def _playable_bit(conn, race_id: int | None) -> int | None:
    if race_id is None:
        return None
    row = conn.execute("SELECT playable_bit FROM races WHERE id = ?", (race_id,)).fetchone()
    return row["playable_bit"] if row else None


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/gear")
async def gear_redirect() -> RedirectResponse:
    return RedirectResponse(url="gear/")


@app.get("/gear/")
async def gear_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "gear" / "index.html")


@app.get("/icon-placeholder.svg")
async def placeholder_icon() -> FileResponse:
    return FileResponse(PLACEHOLDER_ICON, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/icon/{name}")
def item_icon(name: str) -> FileResponse:
    """Self-hosted item icon: /api/icon/<id> or /api/icon/<id>.jpg (the
    frontend uses .jpg so Cloudflare caches it without a custom rule).
    Sync def on purpose — a cold miss blocks on Blizzard, so it runs in the
    threadpool. Unknown/missing icons get the placeholder with a short TTL
    so they're re-checked once Blizzard publishes them."""
    item_id = name.removesuffix(".jpg")
    if not item_id.isdigit():
        raise HTTPException(status_code=404, detail="Not found.")
    path = icons.get(int(item_id))
    if path is None:
        return FileResponse(PLACEHOLDER_ICON, media_type="image/svg+xml",
                            headers={"Cache-Control": "public, max-age=3600"})
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "db_present": db.DB_PATH.exists()}


@app.get("/api/meta")
async def meta() -> dict:
    try:
        with db.get_conn() as conn:
            m = db.read_meta(conn)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return {
        "forever_product": m.get("forever_product"),
        "forever_build": m.get("forever_build"),
        "classic_era_build": m.get("classic_era_build"),
        "refreshed_at": m.get("refreshed_at"),
        "item_count": int(m.get("item_count", 0)),
        "new_count": int(m.get("new_count", 0)),
        "changed_count": int(m.get("changed_count", 0)),
        "unchanged_count": int(m.get("unchanged_count", 0)),
        "stat_match_rate": float(m["stat_match_rate"]) if m.get("stat_match_rate") else None,
        "stat_match_sample": int(m.get("stat_match_sample", 0)),
        "armor_match_rate": float(m["armor_match_rate"]) if m.get("armor_match_rate") else None,
    }


@app.get("/api/classes")
async def classes() -> list[dict]:
    return [{"slug": c["slug"], "name": c["name"], "color": c["color"]} for c in CLASSES]


@app.get("/api/slots")
async def slots() -> list[dict]:
    return [{"slug": s["slug"], "label": s["label"]} for s in SLOTS]


@app.get("/api/items")
async def items(class_: str = Query(..., alias="class"), slot: str = Query(...),
                race: int | None = None, level: int | None = None) -> list[dict]:
    """Class-usable items for a paperdoll slot. With `level`, only items that
    character could equip at that level are listed: required level ≤ level,
    and armor the class has trained by then (plate/mail unlock at 40). With
    `race`, each row says whether the race can use it (flagged, not hidden).
    The off-hand list adds One-Hand weapons for dual-wield classes, and the
    ranged list includes relics (they share the slot in vanilla)."""
    if class_ not in CLASS_BY_SLUG:
        raise HTTPException(status_code=400, detail=f"Unknown class '{class_}'.")
    if slot not in SLOT_BY_SLUG:
        raise HTTPException(status_code=400, detail=f"Unknown slot '{slot}'.")

    where, params = "slot = ?", [slot]
    if slot == "offhand" and class_ in DUAL_WIELD_CLASSES:
        where, params = "(slot = ? OR inventory_type = ?)", [slot, INVTYPE_ONE_HAND]
    elif slot == "ranged":
        where, params = "slot IN (?, ?)", ["ranged", "relic"]

    try:
        with db.get_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM items WHERE {where} ORDER BY item_level DESC, name ASC", params,
            ).fetchall()
            try:
                bit = _playable_bit(conn, race)
            except sqlite3.OperationalError:  # DB from before the set builder; re-run the ingest
                bit = None
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None

    out = []
    for row in rows:
        if not can_equip(class_, row["item_class"], row["item_subclass"], row["allowable_class_mask"]):
            continue
        if level is not None and (row["required_level"] > level or
                                  not armor_trained_by(class_, row["item_class"], row["item_subclass"], level)):
            continue
        summary = _row_to_summary(row)
        if bit is not None:
            summary["race_ok"] = race_allowed(row["allowable_race_mask"], bit)
        out.append(summary)
    return out


@app.get("/api/item/{item_id}")
async def item_detail(item_id: int, debug: bool = False) -> dict:
    try:
        with db.get_conn() as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None

    if row is None:
        raise HTTPException(status_code=404, detail=f"No item with id {item_id}.")

    keys = row.keys()
    item_set = None
    if "item_set" in keys and row["item_set"]:
        with db.get_conn() as conn:
            item_set = _set_payload(conn, row["item_set"])

    return {
        # Use:/Equip:/Chance on hit: lines (ingest/effects.py); None before the effects ingest.
        "effects": json.loads(row["effects_json"]) if "effects_json" in keys and row["effects_json"] else [],
        "set": item_set,
        "id": row["id"],
        "name": row["name"],
        "quality": row["quality"],
        "slot": row["slot"],
        "material": row["material"],
        "required_level": row["required_level"],
        "allowable_class_mask": row["allowable_class_mask"],
        "item_level": row["item_level"],
        "change_status": row["change_status"],
        # .get-style: a DB from before the armor columns existed just shows no armor.
        "armor": row["armor"] if "armor" in row.keys() else None,
        "stats": _public_stats(json.loads(row["stats_json"]), debug),
        "classic_stats": json.loads(row["classic_stats_json"]) if row["classic_stats_json"] else None,
        "classic_item_level": row["classic_item_level"],
        **({"classic_armor": row["classic_armor"] if "classic_armor" in row.keys() else None} if debug else {}),
        "diff": [
            {**d, "forever": _public_stats(d["forever"], debug)} if d["field"] == "stats" else d
            for d in json.loads(row["diff_json"])
        ] if row["diff_json"] else None,
        "wowhead_url": f"https://www.wowhead.com/forever/item={row['id']}",
    }


# ── Set builder ─────────────────────────────────────────────────────────────

MAX_LEVEL = 60


class Loadout(BaseModel):
    class_: str = Field(alias="class")
    race: int
    level: int = Field(ge=1, le=MAX_LEVEL)
    slots: dict[str, int] = {}

    model_config = {"populate_by_name": True}

    @field_validator("class_")
    @classmethod
    def _known_class(cls, v: str) -> str:
        if v not in CLASS_BY_SLUG:
            raise ValueError(f"unknown class {v!r}")
        return v

    @field_validator("slots")
    @classmethod
    def _known_slots(cls, v: dict[str, int]) -> dict[str, int]:
        unknown = set(v) - set(LOADOUT_SLOTS)
        if unknown:
            raise ValueError(f"unknown slots {sorted(unknown)}")
        if any(not 0 < item_id < 10_000_000 for item_id in v.values()):
            raise ValueError("bad item id")
        return v

    def as_dict(self) -> dict:
        return {"class": self.class_, "race": self.race, "level": self.level, "slots": dict(sorted(self.slots.items()))}


def _race_for(conn, loadout: Loadout) -> tuple[dict, bool]:
    """The races row and whether it's a new combo; 400 if the client doesn't offer it."""
    try:
        row = conn.execute(
            "SELECT r.*, cr.is_new_combo FROM races r JOIN class_races cr ON cr.race_id = r.id "
            "WHERE r.id = ? AND cr.class_id = ?",
            (loadout.race, CLASS_BY_SLUG[loadout.class_]["class_id"]),
        ).fetchone()
    except sqlite3.OperationalError:
        raise HTTPException(status_code=503, detail="Character data missing — re-run the ingest.") from None
    if row is None:
        raise HTTPException(status_code=400, detail="That race/class combination isn't available.")
    return dict(row), bool(row["is_new_combo"])


def _item_rows(conn, ids) -> dict[int, dict]:
    ids = list(set(ids))
    if not ids:
        return {}
    rows = conn.execute(f"SELECT * FROM items WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    out = {}
    for row in rows:
        item = dict(row)
        item["stats"] = json.loads(row["stats_json"])
        item["is_shield"] = is_shield(item)
        out[row["id"]] = item
    return out


@app.get("/api/characters")
async def characters() -> dict:
    """Playable races, which classes each can be (from the client's
    CharBaseInfo, including the new combos), and each class's resource."""
    try:
        with db.get_conn() as conn:
            races = [dict(r) for r in conn.execute("SELECT * FROM races ORDER BY faction, id")]
            combos = conn.execute("SELECT class_id, race_id, is_new_combo FROM class_races").fetchall()
            power = {r["class_id"]: r["power_name"] for r in conn.execute("SELECT * FROM class_power")}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except sqlite3.OperationalError:
        raise HTTPException(status_code=503, detail="Character data missing — re-run the ingest.") from None
    slug_by_id = {c["class_id"]: c["slug"] for c in CLASSES}
    by_class: dict[str, list[dict]] = {}
    for row in combos:
        if row["class_id"] in slug_by_id:
            by_class.setdefault(slug_by_id[row["class_id"]], []).append(
                {"race": row["race_id"], "new": bool(row["is_new_combo"])})
    return {
        "races": [{"id": r["id"], "name": r["name"], "faction": r["faction"], "new": bool(r["is_new"])} for r in races],
        "combos": by_class,
        "resource": {slug_by_id[cid]: name for cid, name in power.items() if cid in slug_by_id},
        "max_level": MAX_LEVEL,
        "slots": [{"slot": k, "label": v[0], "browse": v[1]} for k, v in LOADOUT_SLOTS.items()],
    }


@app.post("/api/sheet")
def sheet(loadout: Loadout) -> dict:
    """Equipped items (with per-slot problems) + the computed character sheet."""
    try:
        with db.get_conn() as conn:
            race, is_new_combo = _race_for(conn, loadout)
            class_id = CLASS_BY_SLUG[loadout.class_]["class_id"]
            items_by_id = _item_rows(conn, loadout.slots.values())
            cl = conn.execute("SELECT * FROM class_level WHERE class_id = ? AND level = ?",
                              (class_id, loadout.level)).fetchone()
            power = conn.execute("SELECT power_name FROM class_power WHERE class_id = ?", (class_id,)).fetchone()
            equipped = {slot: items_by_id.get(item_id) for slot, item_id in loadout.slots.items()}
            issues = check(loadout.class_, loadout.level, race["playable_bit"], equipped)
            counted = [item for slot, item in equipped.items()
                       if item and not any(issue["blocks"] for issue in issues.get(slot, []))]
            try:
                set_progress, set_stats = _active_sets(conn, counted)
            except sqlite3.OperationalError:  # DB from before item sets; re-run the ingest
                set_progress, set_stats = [], []
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None

    result = compute_sheet(
        class_slug=loadout.class_, race=race, level=loadout.level, is_new_combo=is_new_combo,
        class_level=dict(cl) if cl else None, power_name=power["power_name"] if power else "Mana",
        items=counted, set_stats=set_stats,
    )
    result["sets"] = set_progress
    return {
        "loadout": loadout.as_dict(),
        "slots": {
            slot: {"item": _row_to_summary_dict(item) if item else None, "item_id": loadout.slots[slot],
                   "issues": issues.get(slot, [])}
            for slot, item in equipped.items()
        },
        "sheet": result,
    }


def _set_payload(conn, set_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM item_sets WHERE id = ?", (set_id,)).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"], "name": row["name"], "placeholder": bool(row["is_placeholder"]),
        "members": json.loads(row["members_json"]),
        "bonuses": [{k: b[k] for k in ("threshold", "text", "revealed")} | {"flat": bool(b["stats"])}
                    for b in json.loads(row["bonuses_json"])],
    }


def _active_sets(conn, items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Set progress for the equipped items, and the flat stats of every
    active bonus. Pieces are counted by distinct item id."""
    pieces: dict[int, set[int]] = {}
    for item in items:
        if item.get("item_set"):
            pieces.setdefault(item["item_set"], set()).add(item["id"])
    progress, flat = [], []
    for set_id, ids in sorted(pieces.items()):
        row = conn.execute("SELECT * FROM item_sets WHERE id = ?", (set_id,)).fetchone()
        if row is None:
            continue
        bonuses = []
        for b in json.loads(row["bonuses_json"]):
            active = len(ids) >= b["threshold"]
            if active and b["stats"]:
                flat += b["stats"]
            bonuses.append({"threshold": b["threshold"], "text": b["text"], "revealed": b["revealed"],
                            "active": active, "flat": bool(b["stats"])})
        progress.append({"id": set_id, "name": row["name"], "placeholder": bool(row["is_placeholder"]),
                         "equipped": len(ids), "total": len(json.loads(row["members_json"])),
                         "equipped_ids": sorted(ids), "bonuses": bonuses})
    return progress, flat


def _row_to_summary_dict(item: dict) -> dict:
    return {k: item[k] for k in ("id", "name", "quality", "slot", "item_level", "required_level", "inventory_type")} | {
        "two_hand": item["inventory_type"] == INVTYPE_TWO_HAND, "unique": bool(item["unique_equipped"])}


def _client_ip(request: Request) -> str:
    # Cloudflare → nginx → here. nginx has no real_ip config, so X-Real-IP is
    # a Cloudflare edge address; CF-Connecting-IP is the actual visitor.
    return (request.headers.get("cf-connecting-ip") or request.headers.get("x-real-ip")
            or (request.client.host if request.client else "unknown"))


@app.post("/api/set")
def save_set(loadout: Loadout, request: Request) -> dict:
    """Save a loadout as a new, permanent, immutable share link."""
    with db.get_conn() as conn:
        _race_for(conn, loadout)  # only valid race/class combos get saved
    try:
        slug = sets.save(loadout.as_dict(), _client_ip(request))
    except sets.RateLimited:
        raise HTTPException(status_code=429, detail=f"Too many saves — limit is {sets.SAVES_PER_HOUR} per hour.") from None
    return {"id": slug, "path": f"gear/set/{slug}"}


@app.get("/api/set/{slug}")
def load_set(slug: str) -> dict:
    loadout = sets.load(slug) if slug.isalnum() and len(slug) <= 16 else None
    if loadout is None:
        raise HTTPException(status_code=404, detail="No saved set with that link.")
    return loadout


@app.get("/gear/set/{slug}")
async def gear_set_page(slug: str) -> FileResponse:
    """Permalink: same page; it reads the slug from its own URL and loads the set."""
    return FileResponse(STATIC_DIR / "gear" / "index.html")


# ── Quest Browser (sub-tool #2) ─────────────────────────────────────────────
# Data: ingest/quests.py. Carryover detail + rewards come from QuestieDB
# (plus the cmangos 1.12 reference for money / choice-vs-guaranteed); new
# Forever quests are known only by id (+ a zone for a few), because quest
# data is server-side and the client's QuestV2 is id-only.

QUEST_SORTS = {"zone": "zone IS NULL, zone, min_level, name", "level": "min_level IS NULL, min_level, zone, name",
               "name": "name IS NULL, name, id", "id": "id",
               "xp": "xp IS NULL, xp DESC, min_level, name",
               "objectives": "objective_count IS NULL, objective_count, xp DESC, name"}
PAGE_MAX = 200


@app.get("/quests")
async def quests_redirect() -> RedirectResponse:
    return RedirectResponse(url="quests/")


@app.get("/quests/")
async def quests_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "quests" / "index.html")


def _quest_conn():
    try:
        return db.get_conn()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


def _quest_summary(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "name": row["name"], "zone": row["zone"], "subzone": row["subzone"],
        "req_level": row["min_level"], "quest_level": row["quest_level"], "faction": row["faction"],
        "status": row["status"], "revealed": bool(row["revealed"]),
        # Full at-level XP (QuestieDB); None when it lists none or the quest isn't revealed.
        "xp": row["xp"] if "xp" in row.keys() else None,
        "objective_count": row["objective_count"] if "objective_count" in row.keys() else None,
    }


@app.get("/api/quests")
def quest_list(zone: str | None = None, min_level: int | None = Query(None, ge=0, le=60),
               max_level: int | None = Query(None, ge=0, le=60), faction: str | None = None,
               forever: str = "all", q: str | None = None, include_unknown: bool = False,
               sort: str = "zone", page: int = Query(1, ge=1), per_page: int = Query(100, ge=1, le=PAGE_MAX)) -> dict:
    """Filtered, paginated quests. faction=alliance|horde lists what that
    faction can take (its own + shared); faction=both lists shared quests
    only. Level/zone/faction filters can't place new quests (their details
    aren't readable), so filtering by them leaves those out. Carryover ids
    the reference doesn't know are hidden unless include_unknown."""
    where, params = [], []
    if forever == "new":
        where.append("status = 'new'")
    elif forever != "all":
        raise HTTPException(status_code=400, detail="forever must be 'new' or 'all'.")
    if not include_unknown:
        where.append("(revealed = 1 OR status = 'new')")
    if zone:
        where.append("zone = ?"); params.append(zone)
    if min_level is not None:
        where.append("min_level >= ?"); params.append(min_level)
    if max_level is not None:
        where.append("min_level <= ?"); params.append(max_level)
    if faction in ("alliance", "horde"):
        where.append("faction IN (?, 'both')"); params.append(faction)
    elif faction == "both":
        where.append("faction = 'both'")
    elif faction:
        raise HTTPException(status_code=400, detail="faction must be alliance, horde or both.")
    if q:
        where.append("(name LIKE ? OR CAST(id AS TEXT) = ?)"); params += [f"%{q}%", q.strip()]
    if sort not in QUEST_SORTS:
        raise HTTPException(status_code=400, detail=f"sort must be one of {sorted(QUEST_SORTS)}.")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    try:
        with _quest_conn() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM quests {clause}", params).fetchone()[0]
            rows = conn.execute(f"SELECT * FROM quests {clause} ORDER BY {QUEST_SORTS[sort]} LIMIT ? OFFSET ?",
                                [*params, per_page, (page - 1) * per_page]).fetchall()
    except sqlite3.OperationalError:
        raise HTTPException(status_code=503, detail="Quest data missing — re-run the ingest.") from None
    return {"total": total, "page": page, "per_page": per_page, "rows": [_quest_summary(r) for r in rows]}


@app.get("/api/quests/zones")
def quest_zones() -> list[dict]:
    try:
        with _quest_conn() as conn:
            rows = conn.execute("SELECT zone, COUNT(*) AS n, SUM(status = 'new') AS new FROM quests "
                                "WHERE zone IS NOT NULL AND (revealed = 1 OR status = 'new') GROUP BY zone ORDER BY zone").fetchall()
    except sqlite3.OperationalError:
        raise HTTPException(status_code=503, detail="Quest data missing — re-run the ingest.") from None
    return [{"zone": r["zone"], "count": r["n"], "new": r["new"]} for r in rows]


@app.get("/api/quests/meta")
def quest_meta() -> dict:
    with _quest_conn() as conn:
        m = db.read_meta(conn)
    return {
        "forever_build": m.get("forever_build"), "classic_era_build": m.get("classic_era_build"),
        "refreshed_at": m.get("refreshed_at"),
        "new_count": int(m.get("quests_new", 0)),
        "classic_detailed": int(m.get("quests_classic_detailed", 0)),
        "classic_unrevealed": int(m.get("quests_classic_unrevealed", 0)),
        "new_with_zone": int(m.get("quests_new_with_zone", 0)),
        "questiedb_version": m.get("quests_questiedb_version"),
        "xp_checked": int(m.get("quests_xp_checked", 0)),
        "xp_matched": int(m.get("quests_xp_matched", 0)),
    }


QUEST_CLASS_BITS = {c["class_id"]: c["name"] for c in CLASSES}


def _quest_text(text: str | None) -> str | None:
    """Vanilla quest text placeholders → readable text ($B line breaks, $N/$C/$R the player)."""
    if not text:
        return None
    text = re.sub(r"\$[gG]\s*([^:;]*):([^;]*);", r"\1/\2", text)
    for token, word in (("$B", "\n"), ("$b", "\n"), ("$N", "<name>"), ("$n", "<name>"),
                        ("$C", "<class>"), ("$c", "<class>"), ("$R", "<race>"), ("$r", "<race>")):
        text = text.replace(token, word)
    return text


@app.get("/api/quest/{quest_id}")
def quest_detail(quest_id: int) -> dict:
    with _quest_conn() as conn:
        row = conn.execute("SELECT * FROM quests WHERE id = ?", (quest_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"No quest with id {quest_id}.")
        return _quest_payload(conn, row)


def _quest_payload(conn, row: sqlite3.Row) -> dict:
    detail = json.loads(row["detail_json"]) if row["detail_json"] else {}
    chain = detail.get("chain", {})
    ids = sorted({abs(q) for v in chain.values() for q in (v if isinstance(v, list) else [v]) if q})
    names = {r["id"]: r["name"] for r in conn.execute(
        f"SELECT id, name FROM quests WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}

    def links(v) -> list[dict]:
        return [{"id": abs(q), "name": names.get(abs(q))} for q in (v if isinstance(v, list) else [v]) if q]

    classes = detail.get("required_classes") or 0
    return {
        **_quest_summary(row),
        "in_client": bool(row["in_client"]),
        "source": row["source"],
        "objectives_text": [_quest_text(t) for t in detail.get("objectives_text", [])],
        "objectives": detail.get("objectives", []),
        "details": _quest_text(detail.get("details")),
        "classes": [name for cid, name in QUEST_CLASS_BITS.items() if classes & (1 << (cid - 1))],
        "started_by": detail.get("started_by"),
        "finished_by": detail.get("finished_by"),
        "chain": {
            "requires": links(chain.get("pre_single", [])),
            "requires_all": links(chain.get("pre_group", [])),
            "leads_to": links(chain.get("next_in_chain")),
            "exclusive_with": links(chain.get("exclusive_to", [])),
            "breadcrumbs": links(chain.get("breadcrumbs", [])),
            "breadcrumb_for": links(chain.get("breadcrumb_for")),
        },
        "quest_line": detail.get("quest_line"), "quest_line_step": detail.get("quest_line_step"),
        # None = not revealed (new Forever quests: server-side until seen in-world).
        "rewards": detail.get("rewards"),
        "wowhead_url": f"https://www.wowhead.com/forever/quest={row['id']}",
    }


# ── Quest guides: ordered quest lists saved as immutable share links ────────
# Stored in sets.db (app/sets.py) next to gear sets, so the ingest's wow.db
# rebuild never touches them. Only quest ids + notes/title are kept; each
# step's quest detail is read live on load.

GUIDE_MAX_STEPS = 200


class GuideStep(BaseModel):
    quest_id: int = Field(gt=0, lt=10_000_000)
    note: str | None = Field(None, max_length=500)


class Guide(BaseModel):
    title: str | None = Field(None, max_length=120)
    faction: str | None = None
    steps: list[GuideStep] = Field(min_length=1, max_length=GUIDE_MAX_STEPS)

    @field_validator("faction")
    @classmethod
    def _known_faction(cls, v: str | None) -> str | None:
        if v not in (None, "alliance", "horde"):
            raise ValueError("faction must be alliance or horde")
        return v

    @field_validator("steps")
    @classmethod
    def _distinct(cls, v: list[GuideStep]) -> list[GuideStep]:
        if len({s.quest_id for s in v}) != len(v):
            raise ValueError("a quest can only appear once in a guide")
        return v

    def as_dict(self) -> dict:
        clean = lambda t: (t or "").strip() or None  # noqa: E731
        return {"title": clean(self.title), "faction": self.faction,
                "steps": [{"quest_id": s.quest_id, "note": clean(s.note)} for s in self.steps]}


@app.post("/api/guide")
def save_guide(guide: Guide, request: Request) -> dict:
    """Save a quest guide as a new, permanent, immutable share link."""
    ids = [s.quest_id for s in guide.steps]
    with _quest_conn() as conn:
        known = {r[0] for r in conn.execute(f"SELECT id FROM quests WHERE id IN ({','.join('?' * len(ids))})", ids)}
    if missing := [i for i in ids if i not in known]:
        raise HTTPException(status_code=400, detail=f"Unknown quest ids: {missing[:10]}")
    try:
        slug = sets.save_guide(guide.as_dict(), _client_ip(request))
    except sets.RateLimited:
        raise HTTPException(status_code=429, detail=f"Too many saves — limit is {sets.SAVES_PER_HOUR} per hour.") from None
    return {"id": slug, "path": f"quests/guide/{slug}"}


@app.get("/api/guide/{slug}")
def load_guide(slug: str) -> dict:
    """The saved guide with each step's current quest detail (None = no longer in the data)."""
    found = sets.load_guide(slug) if slug.isalnum() and len(slug) <= 16 else None
    if found is None:
        raise HTTPException(status_code=404, detail="No saved guide with that link.")
    guide, created_at = found
    with _quest_conn() as conn:
        steps = []
        for step in guide["steps"]:
            row = conn.execute("SELECT * FROM quests WHERE id = ?", (step["quest_id"],)).fetchone()
            steps.append({**step, "quest": _quest_payload(conn, row) if row else None})
    return {"id": slug, "title": guide.get("title"), "faction": guide.get("faction"),
            "created_at": created_at, "steps": steps}


@app.get("/quests/guide/{slug}")
async def quest_guide_page(slug: str) -> FileResponse:
    """Permalink page; it reads the slug from its own URL."""
    return FileResponse(STATIC_DIR / "quests" / "guide.html")


@app.get("/quests/quests.js")
async def quests_js() -> FileResponse:
    """Rendering helpers shared by the Quest Browser and the guide page."""
    return FileResponse(STATIC_DIR / "quests" / "quests.js", media_type="text/javascript")
