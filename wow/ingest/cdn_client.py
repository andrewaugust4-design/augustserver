"""Direct-from-Blizzard-CDN fallback for the wago.tools DB2 CSV export.

**What this is for:** wago.tools sometimes lags a new build by a day or more.
This module pulls the same DB2 tables for a build straight from Blizzard's
CDN and writes CSVs *in wago.tools' exact shape* into the same
``data/raw/<version>/`` cache, so everything downstream (diff, stat/armor
computation, validation, SQLite load) runs unchanged. wago.tools stays the
primary source; this is a parity fallback.

**What this is NOT:** a way past Blizzard's withheld-key encryption. DB2
sections encrypted with a TACT key that isn't public (e.g. unreleased
Forever content) stay encrypted and their rows are skipped, exactly as
wago.tools skips them. Those fill in when Blizzard ships the key and the
community TACTKeys list picks it up — same wall, just no processing lag.

Pieces:
- Build resolution: Blizzard's version service (``us.version.battle.net``,
  with ribbit on ``us.patch.battle.net:1119`` as backup) gives the current
  build's BuildConfig / CDNConfig / ProductConfig hashes. Every build seen is
  recorded in ``data/cdn/builds.json`` — once Blizzard rotates a build off the
  version list, those hashes are the only way back to it.
- Reference data, refreshed each run (small, GitHub-hosted):
  WoWDBDefs (table schemas + table→FileDataID manifest) and TACTKeys.
- Extraction: ``cdnextract/`` — a small .NET tool on TACTSharp + DBCD, the
  same libraries wow.tools.local / wago.tools run. Validated on 1.60.1.70009
  against wago.tools: all 53 Forever tables identical in IDs, columns and
  values (see README "CDN fallback").
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger("wow.ingest")

WOW_DIR = Path(__file__).resolve().parent.parent
CDN_DIR = WOW_DIR / "data" / "cdn"
TACT_CACHE_DIR = CDN_DIR / "cache"        # TACTSharp's CDN file cache (archive indexes etc., ~600 MB)
REF_DIR = CDN_DIR / "ref"
BUILDS_FILE = CDN_DIR / "builds.json"
EXTRACT_BIN = Path(os.environ.get("WOW_CDNEXTRACT_BIN", WOW_DIR / "cdnextract" / "publish" / "cdnextract"))

VERSION_URLS = (
    "https://{region}.version.battle.net/v2/products/{product}/{file}",
    "http://{region}.patch.battle.net:1119/{product}/{file}",
)
WOWDBDEFS_TARBALL = "https://github.com/wowdev/WoWDBDefs/archive/refs/heads/master.tar.gz"
TACTKEYS_URL = "https://raw.githubusercontent.com/wowdev/TACTKeys/master/WoW.txt"
REF_MAX_AGE = 6 * 3600                    # re-sync reference data at most this often
EXTRACT_TIMEOUT = 3600                    # cold cache: ~5 min to pull archive indexes


def _psv(text: str) -> list[dict]:
    """Parse Blizzard's pipe-separated version-service format."""
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("##")]
    header = [h.split("!")[0] for h in lines[0].split("|")]
    return [dict(zip(header, ln.split("|"))) for ln in lines[1:]]


def _version_service(product: str, file: str, region: str) -> list[dict]:
    last_exc: Exception | None = None
    for template in VERSION_URLS:
        url = template.format(region=region, product=product, file=file)
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            return _psv(resp.text)
        except Exception as exc:  # noqa: BLE001 — try the next endpoint
            log.warning("version service %s failed: %s", url, exc)
            last_exc = exc
    raise RuntimeError(f"Blizzard version service unreachable for {product}") from last_exc


def current_build(product: str, region: str = "us") -> dict:
    """The build Blizzard's patch service currently serves for a product:
    ``{product, region, version, build_id, build_config, cdn_config,
    product_config, cdn_path}``. Also recorded in builds.json."""
    rows = _version_service(product, "versions", region)
    row = next((r for r in rows if r.get("Region") == region), None)
    if row is None:
        raise RuntimeError(f"no {region!r} row in {product} versions")
    cdns = _version_service(product, "cdns", region)
    cdn_row = next((r for r in cdns if r.get("Name") == region), cdns[0] if cdns else {})
    build = {
        "product": product,
        "region": region,
        "version": row["VersionsName"],
        "build_id": int(row["BuildId"]),
        "build_config": row["BuildConfig"],
        "cdn_config": row["CDNConfig"],
        "product_config": row.get("ProductConfig", ""),
        "cdn_path": cdn_row.get("Path") or "tpr/wow",
    }
    record_build(build)
    return build


def record_build(build: dict) -> None:
    """Keep every build's config hashes: after Blizzard rotates a build off the
    live version list, these are the only way to extract it again."""
    CDN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        known = json.loads(BUILDS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        known = {}
    key = f"{build['product']}/{build['version']}"
    if key in known:
        return
    known[key] = {**build, "first_seen": datetime.now(timezone.utc).isoformat()}
    tmp = BUILDS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(known, indent=2, sort_keys=True))
    tmp.replace(BUILDS_FILE)
    log.info("Recorded CDN build %s: build_config=%s cdn_config=%s", key, build["build_config"], build["cdn_config"])


def known_build(product: str, version: str) -> dict | None:
    """A previously recorded build (for re-extracting one that's rotated off)."""
    try:
        return json.loads(BUILDS_FILE.read_text()).get(f"{product}/{version}")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _stale(path: Path) -> bool:
    return not path.exists() or time.time() - path.stat().st_mtime > REF_MAX_AGE


def sync_reference(force: bool = False) -> tuple[Path, Path, Path]:
    """Fetch WoWDBDefs (definitions + manifest) and TACTKeys into data/cdn/ref.
    Returns (definitions_dir, manifest_path, keys_path). A failed refresh
    falls back to the previous copy if there is one."""
    REF_DIR.mkdir(parents=True, exist_ok=True)
    defs_dir = REF_DIR / "WoWDBDefs"
    manifest = defs_dir / "manifest.json"
    keys = REF_DIR / "WoW.txt"

    if force or _stale(manifest):
        try:
            log.info("syncing WoWDBDefs from %s", WOWDBDEFS_TARBALL)
            resp = requests.get(WOWDBDEFS_TARBALL, timeout=120)
            resp.raise_for_status()
            staging = REF_DIR / "WoWDBDefs.new"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir()
            with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
                for member in tar.getmembers():
                    # <top>/definitions/X.dbd and <top>/manifest.json only
                    parts = member.name.split("/", 1)
                    if len(parts) != 2 or not member.isfile():
                        continue
                    rel = parts[1]
                    if rel == "manifest.json" or (rel.startswith("definitions/") and rel.count("/") == 1):
                        dest = staging / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(tar.extractfile(member).read())
            if not (staging / "manifest.json").exists():
                raise RuntimeError("WoWDBDefs tarball had no manifest.json")
            shutil.rmtree(defs_dir, ignore_errors=True)
            staging.replace(defs_dir)
        except Exception:
            if not manifest.exists():
                raise
            log.warning("WoWDBDefs refresh failed, using the previous copy", exc_info=True)

    if force or _stale(keys):
        try:
            resp = requests.get(TACTKEYS_URL, timeout=60)
            resp.raise_for_status()
            tmp = keys.with_suffix(".tmp")
            tmp.write_bytes(resp.content)
            tmp.replace(keys)
        except Exception:
            if not keys.exists():
                raise
            log.warning("TACTKeys refresh failed, using the previous copy", exc_info=True)

    return defs_dir / "definitions", manifest, keys


def extract_tables(build: dict, tables: list[str] | tuple[str, ...], cache_dir: Path, force: bool = False) -> Path:
    """Extract ``tables`` for ``build`` into ``cache_dir/<version>/<Table>.csv``
    (the same place and shape ``wago_client.download_csv`` uses). Tables
    already cached are skipped unless ``force``. Raises if any table fails,
    so the ingest aborts and keeps the previous DB."""
    version = build["version"]
    out_dir = cache_dir / version
    wanted = list(dict.fromkeys(tables))
    todo = [t for t in wanted if force or not (out_dir / f"{t}.csv").exists()]
    if not todo:
        log.info("CDN: all %d tables for %s already cached", len(wanted), version)
        return out_dir

    if not EXTRACT_BIN.exists():
        raise RuntimeError(f"cdnextract binary not found at {EXTRACT_BIN} — build it with wow/cdnextract/build.sh")

    defs, manifest, keys = sync_reference()
    out_dir.mkdir(parents=True, exist_ok=True)
    TACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(EXTRACT_BIN),
        "--product", build["product"], "--region", build.get("region", "us"),
        "--buildconfig", build["build_config"], "--cdnconfig", build["cdn_config"],
        "--productconfig", build.get("product_config") or "",
        "--cdnpath", build.get("cdn_path") or "tpr/wow",
        "--version", version,
        "--defs", str(defs), "--manifest", str(manifest), "--keys", str(keys),
        "--cache", str(TACT_CACHE_DIR), "--out", str(out_dir),
    ]
    if os.environ.get("WOW_CDN_EXTRA_HOSTS"):
        cmd += ["--cdns", os.environ["WOW_CDN_EXTRA_HOSTS"]]
    cmd += todo

    log.info("CDN: extracting %d tables for %s %s (build_config=%s)", len(todo), build["product"], version, build["build_config"])
    started = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=EXTRACT_TIMEOUT, cwd=CDN_DIR)
    # TACTSharp is chatty on stdout (and logs transient per-host download
    # retries); keep our own [cdnextract] lines, summarise the rest.
    retries = proc.stdout.count("Failed to download file")
    for line in proc.stderr.splitlines():
        log.info("  %s", line)
    if retries:
        log.info("  (%d transient CDN download retries, each retried on another host)", retries)

    summary_path = out_dir / "_cdn_extract.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    failed = {t: v.get("error") for t, v in summary.get("tables", {}).items() if "error" in v}
    if proc.returncode != 0 or failed:
        tail = "\n".join(proc.stdout.splitlines()[-15:])
        raise RuntimeError(f"cdnextract failed (exit {proc.returncode}) for {version}: {failed or tail}")
    log.info("CDN: extracted %s (%s) in %.0fs", version, summary.get("build_name"), time.monotonic() - started)
    return out_dir


def build_name(cache_dir: Path, version: str) -> str | None:
    """build-name from the last CDN extraction of this version, e.g.
    ``WOW-70009patch1.60.1_ForeverBeta`` (same string wago.tools shows as
    the build description)."""
    try:
        return json.loads((cache_dir / version / "_cdn_extract.json").read_text()).get("build_name")
    except (FileNotFoundError, json.JSONDecodeError):
        return None
