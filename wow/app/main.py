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

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from common.constants import CLASS_BY_SLUG, CLASSES, SLOT_BY_SLUG, SLOTS
from common.proficiency import can_equip

from . import db

app = FastAPI(title="WoW: Forever", docs_url=None, redoc_url=None)

STATIC_DIR = Path(__file__).parent / "static"


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
    }


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/gear")
async def gear_redirect() -> RedirectResponse:
    return RedirectResponse(url="gear/")


@app.get("/gear/")
async def gear_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "gear" / "index.html")


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
    }


@app.get("/api/classes")
async def classes() -> list[dict]:
    return [{"slug": c["slug"], "name": c["name"], "color": c["color"]} for c in CLASSES]


@app.get("/api/slots")
async def slots() -> list[dict]:
    return [{"slug": s["slug"], "label": s["label"]} for s in SLOTS]


@app.get("/api/items")
async def items(class_: str = Query(..., alias="class"), slot: str = Query(...)) -> list[dict]:
    if class_ not in CLASS_BY_SLUG:
        raise HTTPException(status_code=400, detail=f"Unknown class '{class_}'.")
    if slot not in SLOT_BY_SLUG:
        raise HTTPException(status_code=400, detail=f"Unknown slot '{slot}'.")

    try:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM items WHERE slot = ? ORDER BY item_level DESC, name ASC",
                (slot,),
            ).fetchall()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None

    return [
        _row_to_summary(row)
        for row in rows
        if can_equip(class_, row["item_class"], row["item_subclass"], row["allowable_class_mask"])
    ]


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
        "stats": _public_stats(json.loads(row["stats_json"]), debug),
        "classic_stats": json.loads(row["classic_stats_json"]) if row["classic_stats_json"] else None,
        "classic_item_level": row["classic_item_level"],
        "diff": [
            {**d, "forever": _public_stats(d["forever"], debug)} if d["field"] == "stats" else d
            for d in json.loads(row["diff_json"])
        ] if row["diff_json"] else None,
        "wowhead_url": f"https://www.wowhead.com/forever/item={row['id']}",
    }
