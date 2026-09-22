"""WoW: Forever suite — shared data API + Gear Browser (sub-tool #1).

Runs behind a reverse proxy at /wow (see deploy/ for nginx + systemd). All
item data comes from a SQLite DB built by `python -m ingest.run`, which
diffs the Forever beta against Classic Era via the wago.tools DB2 export —
see common/stats.py for how Forever stat values are computed and
ingest/normalize.py for how "changed" is derived. This app never talks to wago.tools itself; it only reads the DB.
"""

import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from common import icons
from common.character import compute_sheet
from common.constants import CLASS_BY_SLUG, CLASSES, SLOT_BY_SLUG, SLOTS
from common.loadout import INVTYPE_ONE_HAND, INVTYPE_TWO_HAND, LOADOUT_SLOTS, check, is_shield, race_allowed
from common.proficiency import DUAL_WIELD_CLASSES, can_equip

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
    """Class-usable items for a paperdoll slot. With `race`/`level`, each row
    also says whether that character can use it now (the set builder flags,
    rather than hides, items the race or level rules out). The off-hand list
    adds One-Hand weapons for dual-wield classes, and the ranged list
    includes relics (they share the slot in vanilla)."""
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
        summary = _row_to_summary(row)
        if bit is not None:
            summary["race_ok"] = race_allowed(row["allowable_race_mask"], bit)
        if level is not None:
            summary["level_ok"] = row["required_level"] <= level
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

    return {
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
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None

    equipped = {slot: items_by_id.get(item_id) for slot, item_id in loadout.slots.items()}
    issues = check(loadout.class_, loadout.level, race["playable_bit"], equipped)
    counted = [item for slot, item in equipped.items()
               if item and not any(issue["blocks"] for issue in issues.get(slot, []))]
    result = compute_sheet(
        class_slug=loadout.class_, race=race, level=loadout.level, is_new_combo=is_new_combo,
        class_level=dict(cl) if cl else None, power_name=power["power_name"] if power else "Mana",
        items=counted,
    )
    return {
        "loadout": loadout.as_dict(),
        "slots": {
            slot: {"item": _row_to_summary_dict(item) if item else None, "item_id": loadout.slots[slot],
                   "issues": issues.get(slot, [])}
            for slot, item in equipped.items()
        },
        "sheet": result,
    }


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
