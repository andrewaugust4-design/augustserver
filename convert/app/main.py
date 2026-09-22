"""Convert — YouTube links or uploaded media to WAV.

One job at a time (protects the CPU shared with Music Review / Image Tools),
polled by the frontend. Output is always 44.1 kHz / 16-bit / stereo WAV.
Results live in DATA_DIR for RESULT_TTL_MIN minutes, then a cleanup thread
removes them. Every external process (yt-dlp, ffmpeg) runs under a hard
timeout so a malformed file or stalled download can't wedge the server.

Requires an upload key (CONVERT_PASSWORD) for all conversions.
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

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

DATA_DIR = Path(os.getenv("DATA_DIR", "/opt/convert/data"))
CONVERT_PASSWORD = os.getenv("CONVERT_PASSWORD", "").strip()
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
MAX_YT_MINUTES = int(os.getenv("MAX_YT_MINUTES", "60"))
RESULT_TTL_MIN = int(os.getenv("RESULT_TTL_MIN", "60"))
JOB_TIMEOUT_S = int(os.getenv("JOB_TIMEOUT_MIN", "30")) * 60
MAX_TOTAL_GB = float(os.getenv("MAX_TOTAL_GB", "5"))

# yt-dlp lives in the same venv as uvicorn, which systemd doesn't put on
# PATH — so resolve it next to the running interpreter by default.
YTDLP = os.getenv("YTDLP_BIN", str(Path(sys.executable).parent / "yt-dlp"))
FFMPEG = os.getenv("FFMPEG_BIN", "ffmpeg")

YOUTUBE_RE = re.compile(
    r"^(https?://)?((www|m|music)\.)?(youtube\.com|youtu\.be)/\S+$", re.IGNORECASE
)

ALLOWED_UPLOAD_EXT = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts", ".m4v", ".wmv",
    ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav", ".wma", ".aiff", ".aif",
}

app = FastAPI(title="Convert", docs_url=None, redoc_url=None)
STATIC_DIR = Path(__file__).parent / "static"

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1)  # strictly one conversion at a time


def _update(job_id: str, **fields) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def _safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip(" ._")
    return (name[:120] or "audio")


def _total_bytes() -> int:
    return sum(f.stat().st_size for f in DATA_DIR.glob("*.wav") if f.is_file())


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


def _ffmpeg_to_wav(src: Path, dest: Path) -> None:
    proc = _run(
        [FFMPEG, "-y", "-i", str(src), "-vn",
         "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", str(dest)],
        timeout=JOB_TIMEOUT_S,
    )
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["unknown ffmpeg error"]
        raise RuntimeError(f"Conversion failed: {tail[0][:300]}")


def _finish(job_id: str, wav_tmp: Path, display_name: str) -> None:
    if _total_bytes() + wav_tmp.stat().st_size > MAX_TOTAL_GB * 1024**3:
        raise RuntimeError(
            "Server storage for converted files is full. Try again later."
        )
    final = DATA_DIR / f"{job_id}.wav"
    shutil.move(str(wav_tmp), final)
    _update(
        job_id,
        status="done",
        stage="done",
        result={
            "name": _safe_name(display_name) + ".wav",
            "size": final.stat().st_size,
            "expires_min": RESULT_TTL_MIN,
        },
    )


def _job_youtube(job_id: str, url: str) -> None:
    tmpdir = Path(tempfile.mkdtemp(prefix="convert_"))
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

        _update(job_id, stage="fetching")
        dl = _run(
            [YTDLP, "--no-playlist", "-f", "bestaudio/best",
             "-o", str(tmpdir / "src.%(ext)s"), url],
            timeout=JOB_TIMEOUT_S,
        )
        if dl.returncode != 0:
            tail = (dl.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
            raise RuntimeError(
                f"Download failed: {tail[0][:300]} "
                "(if this keeps happening, yt-dlp may need updating)"
            )
        sources = sorted(tmpdir.glob("src.*"))
        if not sources:
            raise RuntimeError("Download produced no file.")

        _update(job_id, stage="converting")
        wav_tmp = tmpdir / "out.wav"
        _ffmpeg_to_wav(sources[0], wav_tmp)
        _finish(job_id, wav_tmp, title)
    except subprocess.TimeoutExpired:
        _update(job_id, status="error", error="Job exceeded the time limit and was stopped.")
    except Exception as exc:  # noqa: BLE001
        _update(job_id, status="error", error=str(exc))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _job_file(job_id: str, src_path: str, orig_name: str) -> None:
    tmpdir = Path(tempfile.mkdtemp(prefix="convert_"))
    try:
        _update(job_id, status="running", stage="converting")
        wav_tmp = tmpdir / "out.wav"
        _ffmpeg_to_wav(Path(src_path), wav_tmp)
        _finish(job_id, wav_tmp, Path(orig_name).stem)
    except subprocess.TimeoutExpired:
        _update(job_id, status="error", error="Job exceeded the time limit and was stopped.")
    except Exception as exc:  # noqa: BLE001
        _update(job_id, status="error", error=str(exc))
    finally:
        Path(src_path).unlink(missing_ok=True)
        shutil.rmtree(tmpdir, ignore_errors=True)


def _cleanup_loop() -> None:
    while True:
        time.sleep(600)
        try:
            cutoff = time.time() - RESULT_TTL_MIN * 60
            for f in DATA_DIR.glob("*.wav"):
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            with _jobs_lock:
                stale = [
                    jid for jid, j in _jobs.items()
                    if j.get("created_at", 0) < cutoff and j.get("status") in ("done", "error")
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
        "max_upload_mb": MAX_UPLOAD_MB,
        "max_yt_minutes": MAX_YT_MINUTES,
        "result_ttl_min": RESULT_TTL_MIN,
    }


@app.post("/api/jobs")
def create_job(
    password: str = Form(""),
    url: str = Form(""),
    file: UploadFile | None = File(None),
) -> JSONResponse:
    if not CONVERT_PASSWORD:
        raise HTTPException(
            status_code=503,
            detail="CONVERT_PASSWORD is not set on the server — refusing to "
            "run an open converter.",
        )
    if not secrets.compare_digest(password, CONVERT_PASSWORD):
        raise HTTPException(status_code=401, detail="Wrong or missing upload key.")

    url = url.strip()
    has_file = file is not None and (file.filename or "") != ""
    if bool(url) == has_file:
        raise HTTPException(
            status_code=400, detail="Provide either a YouTube link or a file — one of the two."
        )

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "stage": "queued", "created_at": time.time()}

    if url:
        if not YOUTUBE_RE.match(url):
            with _jobs_lock:
                del _jobs[job_id]
            raise HTTPException(
                status_code=400, detail="That doesn't look like a YouTube link."
            )
        _executor.submit(_job_youtube, job_id, url)
        return JSONResponse({"job_id": job_id})

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_UPLOAD_EXT:
        with _jobs_lock:
            del _jobs[job_id]
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{suffix or 'unknown'}'.",
        )
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="convert_up_")
    written = 0
    try:
        while chunk := file.file.read(1024 * 1024):
            written += len(chunk)
            if written > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"File exceeds the {MAX_UPLOAD_MB} MB limit.",
                )
            tmp.write(chunk)
    except HTTPException:
        tmp.close()
        os.unlink(tmp.name)
        with _jobs_lock:
            del _jobs[job_id]
        raise
    finally:
        tmp.close()
    if written == 0:
        os.unlink(tmp.name)
        with _jobs_lock:
            del _jobs[job_id]
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    _executor.submit(_job_file, job_id, tmp.name, file.filename or "audio")
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
    path = DATA_DIR / f"{job_id}.wav"
    if not path.is_file():
        raise HTTPException(
            status_code=410, detail="This result has expired or does not exist."
        )
    name = (job or {}).get("result", {}).get("name", "audio.wav")
    return FileResponse(path, filename=name, media_type="audio/wav")


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}
