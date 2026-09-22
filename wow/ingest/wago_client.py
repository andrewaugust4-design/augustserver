"""Thin client for the wago.tools build list and DB2-to-CSV export.

No HTML scraping for item data — CSV export is a real, documented endpoint
(`/db2/<Table>/csv?build=<version>`). The one best-effort HTML scrape here
(`describe_build`) only pulls a human-readable build description string for
logging/verification; ingest never depends on it succeeding.
"""

from __future__ import annotations

import csv
import html
import logging
import re
from pathlib import Path

import requests

log = logging.getLogger("wow.ingest")

BUILDS_API = "https://wago.tools/api/builds"
BUILDS_PAGE = "https://wago.tools/builds"
CSV_URL = "https://wago.tools/db2/{table}/csv?build={build}"

REQUEST_TIMEOUT = 30


def get_builds() -> dict[str, list[dict]]:
    resp = requests.get(BUILDS_API, timeout=REQUEST_TIMEOUT, headers={"Accept": "application/json"})
    resp.raise_for_status()
    return resp.json()


def latest_build(product: str, builds: dict[str, list[dict]] | None = None) -> dict:
    """Latest build entry for a product, by created_at (NOT list order —
    wago.tools does not guarantee the list is sorted)."""
    builds = builds if builds is not None else get_builds()
    entries = builds.get(product) or []
    if not entries:
        raise RuntimeError(f"wago.tools returned no builds for product {product!r}")
    return max(entries, key=lambda e: e.get("created_at", ""))


def describe_build(build_config: str) -> str | None:
    """Best-effort: pull the build_config's human description (e.g.
    "WOW-69913patch1.60.1_ForeverBeta") from the /builds page's embedded
    JSON, purely so the ingest log can flag if a product no longer looks
    like what we expect. Returns None on any failure — never fatal."""
    try:
        resp = requests.get(BUILDS_PAGE, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        idx = resp.text.find(build_config)
        if idx == -1:
            return None
        # The build's JSON blob is embedded as an HTML-escaped page prop
        # (&quot; instead of ") rather than JS-string-escaped, so unescape
        # before matching.
        window = html.unescape(resp.text[idx: idx + 1500])
        match = re.search(r'"description"\s*:\s*"([^"]+)"', window)
        return match.group(1) if match else None
    except Exception:  # noqa: BLE001 — logging aid only
        log.warning("describe_build: could not fetch/parse build description", exc_info=True)
        return None


def download_csv(table: str, build_version: str, cache_dir: Path, force: bool = False) -> Path:
    """Download (or reuse a cached copy of) a DB2 table's CSV for a given
    build. Cached under cache_dir/<build_version>/<table>.csv so re-runs on
    an unchanged build don't re-download."""
    build_dir = cache_dir / build_version
    build_dir.mkdir(parents=True, exist_ok=True)
    dest = build_dir / f"{table}.csv"

    if dest.exists() and not force:
        log.info("using cached %s", dest)
        return dest

    url = CSV_URL.format(table=table, build=build_version)
    log.info("downloading %s -> %s", url, dest)
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    if resp.headers.get("content-type", "").split(";")[0].strip() != "text/csv":
        raise RuntimeError(f"unexpected content-type for {url}: {resp.headers.get('content-type')}")

    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(resp.content)
    tmp.replace(dest)
    return dest


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))
