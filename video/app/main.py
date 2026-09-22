"""Video — YouTube video downloader for augustserver.com/video.

Paste a YouTube link, pick a quality cap (1080p/720p/480p), get an MP4.
yt-dlp fetches the best video+audio streams within the cap and ffmpeg
merges them. One job at a time; results live for RESULT_TTL_MIN minutes.

Every external process runs under a hard timeout, and downloads are
bounded three ways: video duration (MAX_YT_MINUTES), per-file size
(MAX_FILE_GB, enforced by yt-dlp), and total storage (MAX_TOTAL_GB).

Requires an upload key (VIDEO_PASSWORD) for all downloads.
"""

import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse

DATA_DIR = Path(os.getenv("DATA_DIR", "/opt/video/data"))
VIDEO_PASSWORD = os.getenv("VIDEO_PASSWORD", "").strip()
MAX_YT_MINUTES = int(os.getenv("MAX_YT_MINUTES", "60"))
RESULT_TTL_MIN = int(os.getenv("RESULT_TTL_MIN", "60"))
JOB_TIMEOUT_S = int(os.getenv("JOB_TIMEOUT_MIN", "30")) * 60
MAX_TOTAL_GB = float(os.getenv("MAX_TOTAL_GB", "5"))
MAX_FILE_GB = float(os.getenv("MAX_FILE_GB", "2"))

YTDLP = os.getenv("YTDLP_BIN", str(Path(sys.executable).parent / "yt-dlp"))

YOUTUBE_RE = re.compile(
    r"^(https?://)?((www|m|music)\.)?(youtube\.com|youtu\.be)/\S+$", re.IGNORECASE
)
QUALITY_CHOICES = {480, 720, 1080}

app = FastAPI(title="Video", docs_url=None, redoc_url=None)
STATIC_DIR = Path(__file__).parent / "static"

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1)


def _update(job_id: str, **fields) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def _safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip(" ._")
    return (name[:120] or "video")


def _total_bytes() -> int:
    return sum(f.stat().st_size for f in DATA_DIR.glob("*.mp4") if f.is_file())


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


def _job_download(job_id: str, url: str, quality: int) -> None:
    tmpdir = Path(tempfile.mkdtemp(prefix="video_"))
    try:
        _update(job_id, status="running", stage="probing")
        probe = _run(
            [YTDLP, "--no-playlist", "--print", "duration", "--print", "title", url],
            timeout=120,
        )
        if probe.returncode != 0:
            tail = (probe.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
            raise RuntimeError(
                f"Could not read that video: {tail[0][:300]} "
                "(if this keeps happening, yt-dlp may need updating)"
            )
        lines = [ln.strip() for ln in probe.stdout.splitlines() if ln.strip()]
        if len(lines) < 2:
            raise RuntimeError("Could not read the video's duration and title.")
        dur_raw, title = lines[0], lines[1]
        try:
            duration = float(dur_raw)
        except ValueError:
            raise RuntimeError(
                "Could not determine the video's duration (live streams "
                "aren't supported)."
            )
        if duration > MAX_YT_MINUTES * 60:
            raise RuntimeError(
                f"Video is {int(duration // 60)} minutes long — the limit "
                f"is {MAX_YT_MINUTES} minutes."
            )

        _update(job_id, stage="downloading")
        fmt = (
            f"bestvideo[height<={quality}]+bestaudio/"
            f"best[height<={quality}]/best"
        )
        max_bytes = int(MAX_FILE_GB * 1024**3)
        dl = _run(
            [YTDLP, "--no-playlist",
             "-f", fmt,
             "--merge-output-format", "mp4",
             "--max-filesize", str(max_bytes),
             "-o", str(tmpdir / "out.%(ext)s"),
             url],
            timeout=JOB_TIMEOUT_S,
        )
        if dl.returncode != 0:
            tail = (dl.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
            raise RuntimeError(
                f"Download failed: {tail[0][:300]} "
                "(if this keeps happening, yt-dlp may need updating)"
            )

        _update(job_id, stage="finalizing")
        outputs = sorted(tmpdir.glob("out.*"))
        if not outputs:
            raise RuntimeError(
                f"Download produced no file — the video may exceed the "
                f"{MAX_FILE_GB:g} GB per-file limit."
            )
        produced = outputs[0]
        if produced.stat().st_size == 0:
            raise RuntimeError("Download produced an empty file.")

        if _total_bytes() + produced.stat().st_size > MAX_TOTAL_GB * 1024**3:
            raise RuntimeError(
                "Server storage for downloaded videos is full. Try again later."
            )

        final = DATA_DIR / f"{job_id}.mp4"
        shutil.move(str(produced), final)
        _update(
            job_id,
            status="done",
            stage="done",
            result={
                "name": _safe_name(title) + ".mp4",
                "size": final.stat().st_size,
                "quality": quality,
                "expires_min": RESULT_TTL_MIN,
            },
        )
    except subprocess.TimeoutExpired:
        _update(job_id, status="error", error="Job exceeded the time limit and was stopped.")
    except Exception as exc:  # noqa: BLE001
        _update(job_id, status="error", error=str(exc))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _cleanup_loop() -> None:
    while True:
        time.sleep(600)
        try:
            cutoff = time.time() - RESULT_TTL_MIN * 60
            for f in DATA_DIR.glob("*.mp4"):
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            with _jobs_lock:
                stale = [
                    jid for jid, j in _jobs.items()
                    if j.get("created_at", 0) < cutoff
                    and j.get("status") in ("done", "error")
                ]
                for jid in stale:
                    del _jobs[jid]
        except Exception:  # noqa: BLE001
            pass


DATA_DIR.mkdir(parents=True, exist_ok=True)
threading.Thread(target=_cleanup_loop, daemon=True).start()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def config() -> dict:
    return {
        "max_yt_minutes": MAX_YT_MINUTES,
        "result_ttl_min": RESULT_TTL_MIN,
        "max_file_gb": MAX_FILE_GB,
    }


@app.post("/api/jobs")
def create_job(
    password: str = Form(""),
    url: str = Form(""),
    quality: int = Form(720),
) -> JSONResponse:
    if not VIDEO_PASSWORD:
        raise HTTPException(
            status_code=503,
            detail="VIDEO_PASSWORD is not set on the server — refusing to "
            "run an open downloader.",
        )
    if not secrets.compare_digest(password, VIDEO_PASSWORD):
        raise HTTPException(status_code=401, detail="Wrong or missing upload key.")

    url = url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="Paste a YouTube link first.")
    if not YOUTUBE_RE.match(url):
        raise HTTPException(status_code=400, detail="That doesn't look like a YouTube link.")
    if quality not in QUALITY_CHOICES:
        raise HTTPException(status_code=400, detail="Invalid quality choice.")

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "stage": "queued", "created_at": time.time()}
    _executor.submit(_job_download, job_id, url, quality)
    return JSONResponse({"job_id": job_id})


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> JSONResponse:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job ID.")
        return JSONResponse(dict(job))


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=404, detail="Not found.")
    with _jobs_lock:
        job = _jobs.get(job_id)
    path = DATA_DIR / f"{job_id}.mp4"
    if not path.is_file():
        raise HTTPException(
            status_code=410, detail="This result has expired or does not exist."
        )
    name = (job or {}).get("result", {}).get("name", "video.mp4")
    return FileResponse(path, filename=name, media_type="video/mp4")


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}
