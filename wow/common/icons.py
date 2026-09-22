"""Self-hosted item icon cache, shared by the API endpoint and the ingest warm.

Icons are fetched from Blizzard's item media API (see common/blizzard.py),
saved once as WOW_ICON_DIR/<itemId>.jpg, and served from disk forever after.
Items the API doesn't know (most Forever-only ids during beta) go into a
small negative cache — its own SQLite file next to the icons, not wow.db,
because ingest rebuilds wow.db and the app opens that read-only — and are
retried only after MISSING_RETRY_SECONDS so coverage fills in toward launch
without re-hitting the API on every page view.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

import requests

from . import blizzard

log = logging.getLogger("wow.icons")

ICON_DIR = Path(os.getenv("WOW_ICON_DIR", Path(__file__).resolve().parent.parent / "data" / "icons"))
MISSING_DB = ICON_DIR / "missing.db"
MISSING_RETRY_SECONDS = 24 * 3600

# Cold-cache page views can ask for a whole slot list at once; don't open
# more than this many simultaneous Blizzard fetches for them.
_fetch_slots = threading.BoundedSemaphore(4)
_id_locks: dict[int, threading.Lock] = {}
_id_locks_guard = threading.Lock()
_warned_unconfigured = False


def icon_path(item_id: int) -> Path:
    return ICON_DIR / f"{item_id}.jpg"


def _missing_conn() -> sqlite3.Connection:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(MISSING_DB, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS missing (item_id INTEGER PRIMARY KEY, checked_at REAL NOT NULL)")
    return conn


def recently_missing(item_id: int) -> bool:
    with _missing_conn() as conn:
        row = conn.execute("SELECT checked_at FROM missing WHERE item_id = ?", (item_id,)).fetchone()
    return row is not None and time.time() - row[0] < MISSING_RETRY_SECONDS


def _mark_missing(item_id: int) -> None:
    with _missing_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO missing (item_id, checked_at) VALUES (?, ?)", (item_id, time.time()))


def _clear_missing(item_id: int) -> None:
    with _missing_conn() as conn:
        conn.execute("DELETE FROM missing WHERE item_id = ?", (item_id,))


def enabled() -> bool:
    """False (with one logged warning) when Blizzard credentials are unset."""
    global _warned_unconfigured
    if blizzard.configured():
        return True
    if not _warned_unconfigured:
        log.warning("BLIZZARD_CLIENT_ID/BLIZZARD_CLIENT_SECRET not set — item icons disabled, serving placeholders.")
        _warned_unconfigured = True
    return False


def fetch(item_id: int) -> bool:
    """Download item_id's icon to disk. True if it's there now; False if
    Blizzard doesn't have it (negative-cached). Transient API/network errors
    propagate as requests.RequestException and are NOT negative-cached."""
    try:
        data = blizzard.download(blizzard.item_icon_url(item_id))
    except blizzard.NotFound:
        _mark_missing(item_id)
        return False
    path = icon_path(item_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)  # atomic, so a concurrent reader never sees a partial file
    _clear_missing(item_id)
    return True


def get(item_id: int) -> Path | None:
    """Path to the cached icon, fetching it on a miss; None → serve a placeholder."""
    path = icon_path(item_id)
    if path.exists():
        return path
    if not enabled() or recently_missing(item_id):
        return None

    with _id_locks_guard:
        lock = _id_locks.setdefault(item_id, threading.Lock())
    with lock:  # two tabs asking for the same cold icon → one fetch
        if path.exists():
            return path
        try:
            with _fetch_slots:
                return path if fetch(item_id) else None
        except requests.RequestException as exc:
            log.warning("Icon fetch for item %s failed (will retry next request): %s", item_id, exc)
            return None
        finally:
            with _id_locks_guard:
                _id_locks.pop(item_id, None)


def warm(item_ids: list[int], rate_per_sec: float = 5.0) -> dict[str, int]:
    """Pre-fetch icons that aren't on disk yet. Resumable by construction:
    anything already saved, or negative-cached within the cooldown, is
    skipped, so an interrupted run just picks up where it left off."""
    counts = {"cached": 0, "fetched": 0, "missing": 0, "skipped_missing": 0, "errors": 0}
    if not enabled():
        return counts
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    interval = 1.0 / rate_per_sec
    for n, item_id in enumerate(item_ids, 1):
        if icon_path(item_id).exists():
            counts["cached"] += 1
            continue
        if recently_missing(item_id):
            counts["skipped_missing"] += 1
            continue
        started = time.monotonic()
        try:
            counts["fetched" if fetch(item_id) else "missing"] += 1
        except requests.RequestException as exc:
            counts["errors"] += 1
            log.warning("Icon warm: item %s failed: %s", item_id, exc)
        if n % 500 == 0:
            log.info("Icon warm progress: %d/%d %s", n, len(item_ids), counts)
        time.sleep(max(0.0, interval - (time.monotonic() - started)))
    return counts
