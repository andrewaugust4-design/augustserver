"""Transcription and language detection using faster-whisper.

Sensitivity is fully tunable via .env:

  WHISPER_VAD=on|off     "on" (default): pass 1 uses a relaxed Silero VAD,
                         with a no-VAD rescue pass if nothing is found.
                         "off": a single no-VAD pass — Whisper hears the
                         entire track, maximum lyric recall.
  VAD_THRESHOLD          Silero speech threshold for VAD passes (default
                         0.30; lower = more permissive toward singing).
  MAX_NO_SPEECH          Drop segments Whisper scores above this
                         "probably not speech" value (default 0.85;
                         raise toward 1.0 to keep almost everything).
  MAX_COMPRESSION        Drop segments above this compression ratio —
                         degenerate repetition (default 2.6; raise to 3.0
                         to only drop extreme cases).
  MIN_SPEECH_SECONDS     Total detected speech below this = instrumental
                         (default 2.0).

Each lyric line is prefixed with a [mm:ss] timestamp of where in the
track it was detected, which makes gaps in detection easy to spot.
"""

import os
import threading

from faster_whisper import WhisperModel

_model = None
_model_lock = threading.Lock()

MIN_SPEECH_SECONDS = float(os.getenv("MIN_SPEECH_SECONDS", "2.0"))
VAD_THRESHOLD = float(os.getenv("VAD_THRESHOLD", "0.30"))
VAD_MODE = os.getenv("WHISPER_VAD", "on").strip().lower()
MAX_NO_SPEECH = float(os.getenv("MAX_NO_SPEECH", "0.85"))
MAX_COMPRESSION = float(os.getenv("MAX_COMPRESSION", "2.6"))

LANGUAGE_NAMES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German",
    "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "ru": "Russian",
    "uk": "Ukrainian", "pl": "Polish", "cs": "Czech", "sv": "Swedish",
    "no": "Norwegian", "da": "Danish", "fi": "Finnish", "tr": "Turkish",
    "ar": "Arabic", "he": "Hebrew", "fa": "Persian", "hi": "Hindi",
    "ur": "Urdu", "bn": "Bengali", "ta": "Tamil", "te": "Telugu",
    "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "vi": "Vietnamese",
    "th": "Thai", "id": "Indonesian", "ms": "Malay", "tl": "Tagalog",
    "el": "Greek", "ro": "Romanian", "hu": "Hungarian", "bg": "Bulgarian",
    "sr": "Serbian", "hr": "Croatian", "sk": "Slovak", "sw": "Swahili",
    "am": "Amharic", "yo": "Yoruba", "ha": "Hausa", "pa": "Punjabi",
}


def _get_model() -> WhisperModel:
    global _model
    with _model_lock:
        if _model is None:
            _model = WhisperModel(
                os.getenv("WHISPER_MODEL", "small"),
                device=os.getenv("WHISPER_DEVICE", "cpu"),
                compute_type=os.getenv("WHISPER_COMPUTE", "int8"),
            )
        return _model


def _collect(segments_iter, *, max_no_speech: float, max_compression: float):
    """Gather segments, dropping likely hallucinations.

    - no_speech_prob: Whisper's own "this probably isn't speech" signal
    - compression_ratio: very high values indicate degenerate repetition
    - consecutive-duplicate lines: classic hallucination-on-music signature
    """
    lines: list[tuple[float, str]] = []
    speech_seconds = 0.0
    prev_text = None
    repeat_run = 0

    for seg in segments_iter:
        if seg.no_speech_prob > max_no_speech:
            continue
        ratio = getattr(seg, "compression_ratio", 0.0) or 0.0
        if ratio > max_compression:
            continue
        text = seg.text.strip()
        if not text:
            continue
        if text == prev_text:
            repeat_run += 1
            # Real choruses repeat, but hallucinations repeat *forever*.
            # Allow up to 3 consecutive identical lines, then drop.
            if repeat_run >= 3:
                continue
        else:
            repeat_run = 0
        prev_text = text
        lines.append((float(seg.start or 0.0), text))
        speech_seconds += max(0.0, seg.end - seg.start)

    # Degenerate output check: a "transcript" that is one identical line
    # repeated (and nothing else) is a hallucination loop, not lyrics.
    if len(lines) >= 2 and len({t for _, t in lines}) == 1:
        return [], 0.0

    return lines, speech_seconds


def _format_timestamp(seconds: float) -> str:
    total = int(seconds)
    return f"[{total // 60:02d}:{total % 60:02d}]"


def transcribe_audio(path: str) -> dict:
    """Transcribe an audio file and detect its language.

    Returns a dict with keys: language, language_confidence, lyrics,
    is_instrumental.
    """
    model = _get_model()

    if VAD_MODE == "off":
        # ---- Single pass, no VAD: Whisper hears the whole track --------
        segments_iter, info = model.transcribe(
            path,
            beam_size=5,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        lines, speech_seconds = _collect(
            segments_iter,
            max_no_speech=MAX_NO_SPEECH,
            max_compression=MAX_COMPRESSION,
        )
    else:
        # ---- Pass 1: relaxed VAD ----------------------------------------
        segments_iter, info = model.transcribe(
            path,
            beam_size=5,
            vad_filter=True,
            vad_parameters={
                "threshold": VAD_THRESHOLD,
                "min_silence_duration_ms": 500,
            },
        )
        lines, speech_seconds = _collect(
            segments_iter,
            max_no_speech=MAX_NO_SPEECH,
            max_compression=MAX_COMPRESSION,
        )

        # ---- Pass 2: no VAD, stricter filtering, if pass 1 found nothing
        if not lines or speech_seconds < MIN_SPEECH_SECONDS:
            segments_iter, info = model.transcribe(
                path,
                beam_size=5,
                vad_filter=False,
                condition_on_previous_text=False,
            )
            lines, speech_seconds = _collect(
                segments_iter,
                max_no_speech=min(MAX_NO_SPEECH, 0.60),
                max_compression=min(MAX_COMPRESSION, 2.4),
            )

    lyrics = "\n".join(
        f"{_format_timestamp(start)} {text}" for start, text in lines
    ).strip()
    is_instrumental = not lyrics or speech_seconds < MIN_SPEECH_SECONDS

    if is_instrumental:
        language = "none / instrumental"
        confidence = None
        lyrics = ""
    else:
        code = (info.language or "").lower()
        language = LANGUAGE_NAMES.get(code, code or "unknown")
        confidence = round(float(info.language_probability or 0.0), 3)

    return {
        "language": language,
        "language_confidence": confidence,
        "lyrics": lyrics,
        "is_instrumental": is_instrumental,
    }
