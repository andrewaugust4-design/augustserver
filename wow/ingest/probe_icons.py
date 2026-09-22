"""Namespace + coverage probe for item icons.

    python -m ingest.probe_icons [--sample 50] [--seed 1]

Takes a random sample of carryover items (present in Classic Era too) and
new Forever-only items from the DB, asks each candidate namespace's item
media endpoint for them, and prints how many resolve to an icon. Use it to
pick BLIZZARD_ITEM_MEDIA_NAMESPACE, and re-run during beta to watch new-item
coverage. Needs BLIZZARD_CLIENT_ID/SECRET in the environment.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys

import requests

from common import blizzard

from .run import DB_PATH

# Real namespaces first; the rest are Forever-ish guesses kept here so the
# probe keeps checking whether Blizzard has started publishing one.
CANDIDATES = (
    "static-classic1x",       # Classic Era (and Anniversary realms)
    "static-classic",         # Classic progression (MoP Classic today)
    "static",                 # retail
    "static-classicforever",
    "static-classic-forever",
    "static-forever",
    "static-classic2x",
    "static-classicplus",
)


def probe(ns: str, ids: list[int]) -> str:
    hits = 0
    for item_id in ids:
        try:
            blizzard.item_icon_url(item_id, ns)
            hits += 1
        except blizzard.NotFound:
            pass
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 403:
                return "no such ns"  # unknown namespaces 403 rather than 404
            raise
    return f"{hits}/{len(ids)}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=50)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    if not blizzard.configured():
        sys.exit("BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET are not set.")

    conn = sqlite3.connect(DB_PATH)
    carry = [r[0] for r in conn.execute("SELECT id FROM items WHERE change_status != 'new' ORDER BY id")]
    new = [r[0] for r in conn.execute("SELECT id FROM items WHERE change_status = 'new' ORDER BY id")]
    rng = random.Random(args.seed)
    carry_s = rng.sample(carry, min(args.sample, len(carry)))
    new_s = rng.sample(new, min(args.sample, len(new)))

    print(f"region={blizzard.REGION}  carryover pool={len(carry)}  new pool={len(new)}\n")
    print(f"{'namespace':<32} {'carryover':>10} {'new':>10}")
    for base in CANDIDATES:
        ns = f"{base}-{blizzard.REGION}"
        print(f"{ns:<32} {probe(ns, carry_s):>10} {probe(ns, new_s):>10}", flush=True)


if __name__ == "__main__":
    main()
