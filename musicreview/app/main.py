"""Music Review — audio analysis web app.

Upload an audio file, get back:
  - detected language (or "none / instrumental")
  - transcribed lyrics
  - explicit / non-explicit flag
  - AcoustID fingerprint matches against the MusicBrainz database

Runs behind a reverse proxy at /musicreview (see deploy/ for nginx + systemd).
"""

import os
import shutil
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .analysis import transcribe_audio
from .explicit import check_explicit
from .fingerprint import lookup_fingerprint

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "100"))
ALLOWED_EXTENSIONS = {
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus",
    ".wma", ".aiff", ".aif", ".mp4", ".webm",
}

app = FastAPI(title="Music Review", docs_url=None, redoc_url=None)

STATIC_DIR = Path(__file__).parent / "static"

# ---------------------------------------------------------------------------
# In-memory job store. Fine for a single-process starter app; swap for Redis
# or a database if you ever run multiple workers.
# ---------------------------------------------------------------------------
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=int(os.getenv("ANALYSIS_WORKERS", "1")))


def _update_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def _run_analysis(job_id: str, audio_path: str, filename: str) -> None:
    try:
        # 1. Transcription + language detection (Whisper)
        _update_job(job_id, status="running", stage="transcribing")
        transcript = transcribe_audio(audio_path)

        # 2. Explicit-content review of the lyrics
        _update_job(job_id, stage="reviewing_lyrics")
        if transcript["is_instrumental"]:
            explicit = {
                "applicable": False,
                "explicit": False,
                "flagged_words": [],
                "note": "No lyrics detected — explicit check not applicable.",
            }
        else:
            explicit = check_explicit(transcript["lyrics"])
            explicit["applicable"] = True

        # 3. Acoustic fingerprint lookup (Chromaprint + AcoustID)
        _update_job(job_id, stage="fingerprinting")
        fingerprint = lookup_fingerprint(audio_path)

        _update_job(
            job_id,
            status="done",
            stage="done",
            result={
                "filename": filename,
                "language": transcript["language"],
                "language_confidence": transcript["language_confidence"],
                "is_instrumental": transcript["is_instrumental"],
                "lyrics": transcript["lyrics"],
                "explicit": explicit,
                "fingerprint": fingerprint,
            },
        )
    except Exception as exc:  # noqa: BLE001 — surface anything to the UI
        _update_job(job_id, status="error", error=str(exc))
    finally:
        try:
            os.unlink(audio_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Routes. All paths are relative to the app root, so the same code works at
# "/" locally and at "/musicreview/" behind nginx (uvicorn --root-path).
# ---------------------------------------------------------------------------
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)) -> JSONResponse:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{suffix or 'unknown'}'. "
            f"Accepted: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="musicreview_")
    written = 0
    try:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"File exceeds the {MAX_UPLOAD_MB} MB upload limit.",
                )
            tmp.write(chunk)
    except HTTPException:
        tmp.close()
        os.unlink(tmp.name)
        raise
    finally:
        tmp.close()

    if written == 0:
        os.unlink(tmp.name)
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "stage": "queued"}
    _executor.submit(_run_analysis, job_id, tmp.name, file.filename or "upload")

    return JSONResponse({"job_id": job_id})


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> JSONResponse:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job ID.")
        return JSONResponse(dict(job))


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}
