"""Read-only SQLite access to the DB ingest/run.py builds."""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.getenv("WOW_DB_PATH", Path(__file__).resolve().parent.parent / "data" / "wow.db"))


@contextmanager
def get_conn():
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"{DB_PATH} does not exist yet — run `python -m ingest.run` once to populate it."
        )
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def read_meta(conn) -> dict:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}
