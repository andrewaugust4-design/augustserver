"""Explicit-content review for transcribed lyrics.

Uses the better-profanity wordlist, optionally extended with a custom list
(one word per line) pointed to by the EXTRA_WORDLIST env var. The check is
word-based: each unique token in the lyrics is tested, and any matches are
returned so the UI can show *why* a track was flagged.
"""

import os
import re
import threading

from better_profanity import profanity

_loaded = False
_load_lock = threading.Lock()

_WORD_RE = re.compile(r"[a-zA-Z'*]+")


def _ensure_loaded() -> None:
    global _loaded
    with _load_lock:
        if _loaded:
            return
        profanity.load_censor_words()
        extra_path = os.getenv("EXTRA_WORDLIST", "")
        if extra_path and os.path.exists(extra_path):
            with open(extra_path, encoding="utf-8") as fh:
                extra = [w.strip() for w in fh if w.strip() and not w.startswith("#")]
            if extra:
                profanity.add_censor_words(extra)
        _loaded = True


def check_explicit(lyrics: str) -> dict:
    """Return {'explicit': bool, 'flagged_words': [...], 'note': str}."""
    _ensure_loaded()

    tokens = {t.lower() for t in _WORD_RE.findall(lyrics)}
    flagged = sorted(t for t in tokens if profanity.contains_profanity(t))

    # Catch multi-word profanity that single tokens miss.
    if not flagged and profanity.contains_profanity(lyrics):
        flagged = ["(phrase-level match)"]

    return {
        "explicit": bool(flagged),
        "flagged_words": flagged,
        "note": (
            "Flagged words found in the transcribed lyrics."
            if flagged
            else "No explicit words found in the transcribed lyrics."
        ),
    }
