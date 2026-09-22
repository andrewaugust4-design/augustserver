"""Acoustic fingerprinting via Chromaprint (fpcalc) + the AcoustID API.

Requires:
  - the fpcalc binary:  sudo apt install libchromaprint-tools
  - a free AcoustID application key:  https://acoustid.org/new-application
    (set it as ACOUSTID_API_KEY)

Important scope note, reflected in the UI: AcoustID matches *recordings*
that exist in the MusicBrainz database. It will catch an upload that
contains a known released recording, but it is not a sample/beat detector —
a re-recorded, pitched, or unreleased beat will not reliably match.
"""

import os

import acoustid

MIN_SCORE = float(os.getenv("ACOUSTID_MIN_SCORE", "0.5"))
MAX_MATCHES = int(os.getenv("ACOUSTID_MAX_MATCHES", "8"))


def lookup_fingerprint(path: str) -> dict:
    api_key = os.getenv("ACOUSTID_API_KEY", "").strip()
    if not api_key:
        return {
            "available": False,
            "matches": [],
            "note": "ACOUSTID_API_KEY is not set. Get a free key at "
            "acoustid.org/new-application and add it to your .env file.",
        }

    try:
        duration, fingerprint = acoustid.fingerprint_file(path)
    except acoustid.NoBackendError:
        return {
            "available": False,
            "matches": [],
            "note": "fpcalc not found. Install it with: "
            "sudo apt install libchromaprint-tools",
        }
    except acoustid.FingerprintGenerationError as exc:
        return {
            "available": False,
            "matches": [],
            "note": f"Could not fingerprint this file: {exc}",
        }

    try:
        response = acoustid.lookup(
            api_key, fingerprint, duration, meta="recordings releasegroups"
        )
    except acoustid.WebServiceError as exc:
        return {
            "available": False,
            "matches": [],
            "note": f"AcoustID lookup failed: {exc}",
        }

    matches: list[dict] = []
    seen: set[str] = set()
    for result in response.get("results", []):
        score = float(result.get("score", 0.0))
        if score < MIN_SCORE:
            continue
        for rec in result.get("recordings", []) or []:
            rec_id = rec.get("id", "")
            if not rec_id or rec_id in seen:
                continue
            seen.add(rec_id)
            artists = ", ".join(
                a.get("name", "") for a in rec.get("artists", []) or []
            )
            matches.append(
                {
                    "score": round(score, 3),
                    "title": rec.get("title") or "(untitled recording)",
                    "artists": artists or "(unknown artist)",
                    "musicbrainz_url": f"https://musicbrainz.org/recording/{rec_id}",
                }
            )

    matches.sort(key=lambda m: m["score"], reverse=True)
    matches = matches[:MAX_MATCHES]

    return {
        "available": True,
        "matches": matches,
        "note": (
            "Match(es) found in the MusicBrainz database — this audio appears "
            "to contain a known released recording."
            if matches
            else "No matches found among known released recordings. Note: this "
            "does not guarantee the beat is unused — fingerprinting only "
            "catches recordings present in the MusicBrainz database."
        ),
    }
