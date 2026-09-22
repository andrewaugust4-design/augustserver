"""Safety net for the Forever stat formula (common/stats.py): before a
refresh is written, recompute every shared item's stats and compare them
with the real numbers Classic Era stores.

Items the diff marks `unchanged` have the same stat ids, level, quality and
slot in both builds, so if the budget column/slot selection is right their
computed values should almost all equal Classic's (~99.7% on 2026-09-22 —
the rest are items Forever retuned, where the computed value is the Forever
one). A big drop means Blizzard changed the budget table or the selection
logic is wrong, and the refresh is refused rather than shipping bad numbers.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict

from common.stats import PRIMARY_STAT_IDS, RESISTANCE_STAT_IDS, STAT_INFO

log = logging.getLogger("wow.ingest")

MIN_MATCH_RATE = 0.95

# Fiery Slippers (rare feet, ilvl 35 -> Superior_1 budget 15): the reference
# tooltip is +7 Int, +6 Stam, +4 Spirit, Equip: +13 fire spell damage.
ANCHOR_ITEM_ID = 254005
ANCHOR_EXPECTED = {5: 7, 7: 6, 6: 4, 85: 13}


class ValidationError(RuntimeError):
    pass


def _match_counts(items: dict[int, dict], status: str, stat_ids: frozenset[int] | None) -> tuple[int, int]:
    matched = total = 0
    for item in items.values():
        if item["change_status"] != status:
            continue
        classic = {s["stat_id"]: s["value"] for s in item.get("classic_stats") or []}
        for s in item["stats"]:
            if s["stat_id"] not in classic or s["value"] is None:
                continue
            if stat_ids is not None and s["stat_id"] not in stat_ids:
                continue
            total += 1
            matched += s["value"] == classic[s["stat_id"]]
    return matched, total


def _rate(matched: int, total: int) -> float:
    return matched / total if total else 0.0


def validate(items: dict[int, dict]) -> dict[str, str]:
    """Log the match rates + unmapped stat ids and raise ValidationError if
    the formula looks broken. Returns meta entries to store with the build."""
    report = {}
    for label, ids in (("primary", PRIMARY_STAT_IDS), ("resistance", RESISTANCE_STAT_IDS), ("all", None)):
        m, t = _match_counts(items, "unchanged", ids)
        log.info("Stat formula check (unchanged items, %s stats): %d/%d match Classic Era (%.2f%%)",
                 label, m, t, 100 * _rate(m, t))
        report[label] = (m, t)
    m, t = _match_counts(items, "changed", None)
    log.info("Stat formula check (changed items, informational): %d/%d match Classic Era (%.2f%%)",
             m, t, 100 * _rate(m, t))

    no_budget = sum(1 for it in items.values() if any(s["value"] is None for s in it["stats"]))
    if no_budget:
        log.warning("%d items have stats but no stat budget (unknown level/quality/slot) — shown without values", no_budget)

    unmapped: dict[int, list[str]] = defaultdict(list)
    for it in items.values():
        for s in it["stats"]:
            if s["stat_id"] not in STAT_INFO:
                unmapped[s["stat_id"]].append(it["name"])
    for stat_id, names in sorted(unmapped.items()):
        log.warning("Unmapped stat id %d on %d items (shown as 'Stat %d'), e.g. %s",
                    stat_id, len(names), stat_id, ", ".join(names[:3]))

    anchor = items.get(ANCHOR_ITEM_ID)
    if anchor is None:
        log.warning("Anchor item %d (Fiery Slippers) not in this build — skipping anchor check", ANCHOR_ITEM_ID)
    else:
        got = {s["stat_id"]: s["value"] for s in anchor["stats"]}
        if got != ANCHOR_EXPECTED:
            raise ValidationError(f"Anchor item {ANCHOR_ITEM_ID} ({anchor['name']}) computed {got}, expected {ANCHOR_EXPECTED}")
        log.info("Anchor item %d (%s) matches: %s", ANCHOR_ITEM_ID, anchor["name"], got)

    m, t = report["primary"]
    if _rate(m, t) < MIN_MATCH_RATE:
        raise ValidationError(
            f"Only {m}/{t} ({100 * _rate(m, t):.2f}%) primary stats on unchanged items match Classic Era "
            f"(need {100 * MIN_MATCH_RATE:.0f}%) — RandPropPoints selection may be wrong; refusing to write."
        )
    m, t = report["all"]
    return {"stat_match_rate": f"{_rate(m, t):.4f}", "stat_match_sample": str(t)}
