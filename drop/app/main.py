"""Drop — temporary file sharing for augustserver.com/drop.

Upload a file, get an unguessable link. Files expire by time (1h–7d) and
optionally by download count (including burn-after-one-download). A
background thread purges expired files; exhausted files are deleted right
after their final download finishes streaming.

Files are stored as-is on disk (NOT encrypted — anyone with the link can
download). Set UPLOAD_PASSWORD in .env to require a key for uploading.
"""

import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

DATA_DIR = Path(os.getenv("DATA_DIR", "/opt/drop/data"))
DB_PATH = DATA_DIR / "drop.db"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "200"))
MAX_TOTAL_GB = float(os.getenv("MAX_TOTAL_GB", "5"))
UPLOAD_PASSWORD = os.getenv("UPLOAD_PASSWORD", "").strip()
CLEANUP_INTERVAL_S = int(os.getenv("CLEANUP_INTERVAL_MIN", "10")) * 60

# Allowed values from the UI. hours -> label only; 0 downloads = unlimited.
EXPIRY_CHOICES = {1, 24, 72, 168}
DOWNLOAD_CHOICES = {0, 1, 5, 10}

app = FastAPI(title="Drop", docs_url=None, redoc_url=None)

STATIC_DIR = Path(__file__).parent / "static"

_db_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS files (
                id            TEXT PRIMARY KEY,
                orig_name     TEXT NOT NULL,
                size          INTEGER NOT NULL,
                created_at    REAL NOT NULL,
                expires_at    REAL NOT NULL,
                max_downloads INTEGER NOT NULL,
                downloads     INTEGER NOT NULL DEFAULT 0
            )"""
        )


_init()


def _delete_entry(conn: sqlite3.Connection, file_id: str) -> None:
    (DATA_DIR / file_id).unlink(missing_ok=True)
    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))


def _purge_expired() -> None:
    now = time.time()
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT id FROM files WHERE expires_at < ?", (now,)
        ).fetchall()
        for row in rows:
            _delete_entry(conn, row["id"])


def _cleanup_loop() -> None:
    while True:
        time.sleep(CLEANUP_INTERVAL_S)
        try:
            _purge_expired()
        except Exception:  # noqa: BLE001 — never let the janitor die
            pass


threading.Thread(target=_cleanup_loop, daemon=True).start()


def _total_bytes(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(SUM(size), 0) AS s FROM files").fetchone()
    return int(row["s"])


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def config() -> dict:
    return {
        "max_upload_mb": MAX_UPLOAD_MB,
        "password_required": bool(UPLOAD_PASSWORD),
    }


@app.post("/api/upload")
def upload(
    file: UploadFile = File(...),
    expiry_hours: int = Form(24),
    max_downloads: int = Form(0),
    password: str = Form(""),
) -> JSONResponse:
    if UPLOAD_PASSWORD and not secrets.compare_digest(password, UPLOAD_PASSWORD):
        raise HTTPException(status_code=401, detail="Wrong or missing upload key.")
    if expiry_hours not in EXPIRY_CHOICES:
        raise HTTPException(status_code=400, detail="Invalid expiry choice.")
    if max_downloads not in DOWNLOAD_CHOICES:
        raise HTTPException(status_code=400, detail="Invalid download-limit choice.")

    orig_name = re.sub(r"[\r\n\"\\]", "_", (file.filename or "file"))[:255] or "file"

    file_id = secrets.token_urlsafe(8)
    dest = DATA_DIR / file_id
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024

    written = 0
    try:
        with open(dest, "wb") as out:
            while chunk := file.file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds the {MAX_UPLOAD_MB} MB limit.",
                    )
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise

    if written == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    now = time.time()
    with _db_lock, _db() as conn:
        if _total_bytes(conn) + written > MAX_TOTAL_GB * 1024**3:
            dest.unlink(missing_ok=True)
            raise HTTPException(
                status_code=507,
                detail="Server storage for shared files is full. Try again later.",
            )
        conn.execute(
            "INSERT INTO files (id, orig_name, size, created_at, expires_at, "
            "max_downloads, downloads) VALUES (?, ?, ?, ?, ?, ?, 0)",
            (file_id, orig_name, written, now, now + expiry_hours * 3600, max_downloads),
        )

    return JSONResponse(
        {
            "id": file_id,
            "name": orig_name,
            "size": written,
            "expiry_hours": expiry_hours,
            "max_downloads": max_downloads,
        }
    )


@app.get("/f/{file_id}")
def download(file_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", file_id):
        raise HTTPException(status_code=404, detail="Not found.")

    now = time.time()
    with _db_lock, _db() as conn:
        row = conn.execute(
            "SELECT * FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Not found.")
        if row["expires_at"] < now:
            _delete_entry(conn, file_id)
            raise HTTPException(status_code=410, detail="This file has expired.")
        if row["max_downloads"] and row["downloads"] >= row["max_downloads"]:
            _delete_entry(conn, file_id)
            raise HTTPException(
                status_code=410, detail="This file's download limit was reached."
            )
        conn.execute(
            "UPDATE files SET downloads = downloads + 1 WHERE id = ?", (file_id,)
        )
        exhausted = bool(
            row["max_downloads"] and row["downloads"] + 1 >= row["max_downloads"]
        )
        orig_name = row["orig_name"]

    path = DATA_DIR / file_id
    if not path.is_file():
        with _db_lock, _db() as conn:
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        raise HTTPException(status_code=410, detail="This file is no longer available.")

    task = BackgroundTask(_remove_after_send, file_id) if exhausted else None
    return FileResponse(
        path,
        filename=orig_name,
        media_type="application/octet-stream",
        background=task,
    )


def _remove_after_send(file_id: str) -> None:
    with _db_lock, _db() as conn:
        _delete_entry(conn, file_id)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}
