"""Minimal Battle.net Game Data API client — just what item icons need.

OAuth client-credentials token (cached until shortly before its ~24h expiry)
and the item media lookup. Credentials come from BLIZZARD_CLIENT_ID /
BLIZZARD_CLIENT_SECRET (a free client registered at develop.battle.net);
`configured()` is False without them and callers are expected to fall back
to placeholders rather than fail.

Namespace: there is no Forever namespace. Blizzard doesn't expose beta/PTR
data through the API, and every guessed Forever-ish name
(static-classicforever-us, static-forever-us, …) returns 403 exactly like a
made-up one. `python -m ingest.probe_icons` measures the real candidates;
on 2026-09-22 it picked static-classic1x (Classic Era) — see README.
"""

from __future__ import annotations

import os
import threading
import time

import requests

REGION = os.getenv("BLIZZARD_REGION", "us")
DEFAULT_NAMESPACE_BASE = "static-classic1x"

TOKEN_URL = "https://oauth.battle.net/token"
MEDIA_URL = "https://{region}.api.blizzard.com/data/wow/media/item/{item_id}"

REQUEST_TIMEOUT = 15


class NotFound(Exception):
    """The API (or the render CDN) has no icon for this item."""


def _client_id() -> str:
    return os.getenv("BLIZZARD_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.getenv("BLIZZARD_CLIENT_SECRET", "").strip()


def configured() -> bool:
    return bool(_client_id() and _client_secret())


def namespace() -> str:
    return os.getenv("BLIZZARD_ITEM_MEDIA_NAMESPACE") or f"{DEFAULT_NAMESPACE_BASE}-{REGION}"


_session = requests.Session()
_token_lock = threading.Lock()
_token: str | None = None
_token_expires = 0.0


def _get_token(force: bool = False) -> str:
    global _token, _token_expires
    with _token_lock:
        if not force and _token and time.time() < _token_expires:
            return _token
        resp = _session.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(_client_id(), _client_secret()),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        _token = body["access_token"]
        # Refresh 5 minutes early so a request never races the expiry.
        _token_expires = time.time() + int(body.get("expires_in", 86400)) - 300
        return _token


def _api_get(url: str, params: dict) -> requests.Response:
    resp = _session.get(url, params=params, headers={"Authorization": f"Bearer {_get_token()}"},
                        timeout=REQUEST_TIMEOUT)
    if resp.status_code == 401:  # token revoked/rotated early — retry once with a fresh one
        resp = _session.get(url, params=params, headers={"Authorization": f"Bearer {_get_token(force=True)}"},
                            timeout=REQUEST_TIMEOUT)
    return resp


def item_icon_url(item_id: int, ns: str | None = None) -> str:
    """render.worldofwarcraft.com URL of the item's icon. Raises NotFound on
    a 404 (item not in that namespace, or no icon asset); other HTTP errors
    raise requests.HTTPError so callers can treat them as transient."""
    resp = _api_get(
        MEDIA_URL.format(region=REGION, item_id=item_id),
        {"namespace": ns or namespace(), "locale": "en_US"},
    )
    if resp.status_code == 404:
        raise NotFound(item_id)
    resp.raise_for_status()
    for asset in resp.json().get("assets", []):
        if asset.get("key") == "icon" and asset.get("value"):
            return asset["value"]
    raise NotFound(item_id)


def download(url: str) -> bytes:
    resp = _session.get(url, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 404:
        raise NotFound(url)
    resp.raise_for_status()
    return resp.content
