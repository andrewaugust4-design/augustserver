"""Image Tools — background remover backend.

POST an image, get back a transparent PNG with the background removed.
Uses rembg (U^2-Net) running locally on CPU — images never leave the server.

The model (~170 MB) downloads on first use into U2NET_HOME, which the
.env/systemd setup points at /opt/imagetools/.cache so the www-data user
can write it (same pattern as Music Review's Whisper cache).
"""

import os
import threading
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}

app = FastAPI(title="Image Tools", docs_url=None, redoc_url=None)

STATIC_DIR = Path(__file__).parent / "static"

_session = None
_session_lock = threading.Lock()


def _get_session():
    """Load the rembg model once, lazily, thread-safe."""
    global _session
    with _session_lock:
        if _session is None:
            from rembg import new_session

            _session = new_session(os.getenv("REMBG_MODEL", "u2net"))
        return _session


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/remove")
def remove_background(file: UploadFile = File(...)) -> Response:
    # Sync handler on purpose: FastAPI runs it in a worker thread, so the
    # CPU-bound inference never blocks the event loop.
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{suffix or 'unknown'}'. "
            f"Accepted: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    chunks = []
    total = 0
    while chunk := file.file.read(1024 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds the {MAX_UPLOAD_MB} MB limit.",
            )
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    from rembg import remove

    try:
        output = remove(data, session=_get_session())
    except Exception as exc:  # noqa: BLE001 — surface anything to the UI
        raise HTTPException(status_code=500, detail=f"Processing failed: {exc}")

    stem = Path(file.filename or "image").stem or "image"
    return Response(
        content=output,
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{stem}_nobg.png"'},
    )


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}
