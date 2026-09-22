"""Saved set-builder loadouts: immutable, permanent, public share links.

Lives in its own SQLite file (WOW_SETS_DB, default data/sets.db) because the
ingest rebuilds wow.db on every refresh; nothing in ingest/ touches this
file. Each save mints a new random slug; rows are never updated, so a shared
link never changes under anyone. Only class/race/level + item ids are
stored; stats are recomputed on load.

Saves are open to anyone and rate-limited per client IP (hashed, so the
table doesn't keep raw addresses).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

SETS_DB = Path(os.getenv("WOW_SETS_DB", Path(__file__).resolve().parent.parent / "data" / "sets.db"))
SAVES_PER_HOUR = int(os.getenv("WOW_SET_SAVES_PER_HOUR", "30"))
SLUG_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/l/I lookalikes
SLUG_LENGTH = 8


class RateLimited(Exception):
    pass


def _conn() -> sqlite3.Connection:
    SETS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SETS_DB, timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS sets (
        id TEXT PRIMARY KEY,
        loadout_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )""")
    conn.execute("CREATE TABLE IF NOT EXISTS saves (ip_hash TEXT NOT NULL, at REAL NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_saves_ip ON saves(ip_hash, at)")
    return conn


def _ip_hash(ip: str) -> str:
    return hashlib.sha256(f"wow-sets:{ip}".encode()).hexdigest()[:24]


def save(loadout: dict, client_ip: str) -> str:
    now = time.time()
    ip_hash = _ip_hash(client_ip)
    with _conn() as conn:
        conn.execute("DELETE FROM saves WHERE at < ?", (now - 3600,))
        (recent,) = conn.execute("SELECT COUNT(*) FROM saves WHERE ip_hash = ?", (ip_hash,)).fetchone()
        if recent >= SAVES_PER_HOUR:
            raise RateLimited
        payload = json.dumps(loadout, separators=(",", ":"), sort_keys=True)
        while True:
            slug = "".join(secrets.choice(SLUG_ALPHABET) for _ in range(SLUG_LENGTH))
            try:
                conn.execute("INSERT INTO sets (id, loadout_json, created_at) VALUES (?, ?, ?)", (slug, payload, now))
                break
            except sqlite3.IntegrityError:
                continue  # 56^8 ids; a collision just means roll again
        conn.execute("INSERT INTO saves (ip_hash, at) VALUES (?, ?)", (ip_hash, now))
    return slug


def load(slug: str) -> dict | None:
    if not SETS_DB.exists():
        return None
    with _conn() as conn:
        row = conn.execute("SELECT loadout_json FROM sets WHERE id = ?", (slug,)).fetchone()
    return json.loads(row[0]) if row else None
