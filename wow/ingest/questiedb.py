"""QuestieDB: open community quest/NPC/item/object data for Classic, used for
carryover quest detail and rewards. https://github.com/Questie/QuestieDB

Downloaded from GitHub at ingest time: plain file downloads of the repo's
Source-mode data, pinned to the current master commit and cached under
data/raw/questiedb/<sha>/. So a refresh picks up QuestieDB fixes, and an
unchanged commit re-uses the cache. If GitHub is unreachable, the newest
cached commit is used. Parsed with ingest/luadata.py; nothing is executed.

What we take (field numbers are the files' own questKeys/itemKeys/npcKeys):
- quests (Classic Era): name, startedBy/finishedBy (NPC/object/item),
  required + quest level, requiredRaces/Classes, objectivesText,
  objectives, chain fields (preQuestSingle/Group, nextQuestInChain,
  exclusiveTo, breadcrumbs), zoneOrSort, reputationReward
- items: name, and questRewards (item → quests), inverted to give each
  quest's reward items
- npcs/objects: names (+ NPC zone), for quest givers / turn-ins / objectives
- QuestXP: quest XP

QuestieDB has no money reward and doesn't say which reward items are
"choose one" vs guaranteed. ingest/quests.py takes those two from the
cmangos 1.12 reference, whose reward item sets match QuestieDB's on 1,792
of 1,793 quests. Note QuestieDB's own "Forever" flavor is today Classic
data re-projected to Forever's map coordinates. It has no new Forever quests,
which is why the New-to-Forever diff comes from the client instead.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

import requests

from .luadata import assigned_table, embedded_data

log = logging.getLogger("wow.ingest")

REPO = "Questie/QuestieDB"
FILES = {
    "quests": "data/Classic/classicQuestDB.lua",
    "items": "data/Classic/classicItemDB.lua",
    "npcs": "data/Classic/classicNpcDB.lua",
    "objects": "data/Classic/classicObjectDB.lua",
    "xp": "support/QuestXP/xpDB-classic.lua",
    "toc": "QuestieDB.toc",
}
TIMEOUT = 60


def fetch(cache_root: Path) -> Path:
    """Download (or reuse) the pinned files; returns their directory."""
    cache_root.mkdir(parents=True, exist_ok=True)
    try:
        resp = requests.get(f"https://api.github.com/repos/{REPO}/commits/master", timeout=TIMEOUT,
                            headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        sha = resp.json()["sha"]
    except (requests.RequestException, KeyError, ValueError) as exc:
        cached = sorted((p for p in cache_root.iterdir() if (p / ".complete").exists()),
                        key=lambda p: p.stat().st_mtime)
        if not cached:
            raise RuntimeError(f"QuestieDB unreachable and nothing cached: {exc}") from exc
        log.warning("QuestieDB commit lookup failed (%s) — using cached %s", exc, cached[-1].name)
        return cached[-1]

    target = cache_root / sha
    if (target / ".complete").exists():
        log.info("using cached QuestieDB %s", sha[:10])
        return target
    target.mkdir(exist_ok=True)
    for path in FILES.values():
        url = f"https://raw.githubusercontent.com/{REPO}/{sha}/{path}"
        log.info("downloading %s", url)
        r = requests.get(url, timeout=TIMEOUT * 2)
        r.raise_for_status()
        (target / Path(path).name).write_bytes(r.content)
    (target / ".complete").write_text(sha)
    return target


def version(qdir: Path) -> str:
    toc = (qdir / "QuestieDB.toc").read_text(encoding="utf-8", errors="replace")
    ver = next((line.split(":", 1)[1].strip() for line in toc.splitlines()
                if line.lower().startswith("## version:")), "?")
    return f"{ver} ({qdir.name[:7]})"


def _read(qdir: Path, key: str) -> str:
    return (qdir / Path(FILES[key]).name).read_text(encoding="utf-8")


def load(qdir: Path) -> dict:
    """Quests keyed by field *name* (via the file's questKeys), plus lookups."""
    quest_src = _read(qdir, "quests")
    qkeys = {name: idx for name, idx in assigned_table(quest_src, "QuestieDB.questKeys").items()}
    raw_quests = embedded_data(quest_src, "QuestieDB.questData")
    quests = {qid: {name: row.get(idx) for name, idx in qkeys.items()} for qid, row in raw_quests.items()}

    item_src = _read(qdir, "items")
    ikeys = assigned_table(item_src, "QuestieDB.itemKeys")
    items = embedded_data(item_src, "QuestieDB.itemData")
    item_names = {iid: it.get(ikeys["name"]) for iid, it in items.items()}
    reward_items: dict[int, list[int]] = defaultdict(list)
    for iid, it in items.items():
        for qid in (it.get(ikeys["questRewards"]) or {}).values():
            reward_items[qid].append(iid)

    npc_src = _read(qdir, "npcs")
    nkeys = assigned_table(npc_src, "QuestieDB.npcKeys")
    npcs = {nid: (n.get(nkeys["name"]), n.get(nkeys["zoneID"])) for nid, n in embedded_data(npc_src, "QuestieDB.npcData").items()}

    obj_src = _read(qdir, "objects")
    okeys = assigned_table(obj_src, "QuestieDB.objectKeys")
    objects = {oid: o.get(okeys["name"]) for oid, o in embedded_data(obj_src, "QuestieDB.objectData").items()}

    # QuestXP.db rows are {level, xp}: the full at-level reward and the level it's tiered by.
    xp_rows = assigned_table(_read(qdir, "xp"), "QuestXP.db")
    xp = {qid: row.get(2) for qid, row in xp_rows.items()}
    xp_level = {qid: row.get(1) for qid, row in xp_rows.items()}
    return {"quests": quests, "item_names": item_names, "reward_items": dict(reward_items),
            "npcs": npcs, "objects": objects, "xp": xp, "xp_level": xp_level, "version": version(qdir)}


def values(tbl) -> list:
    """A positional Lua table (dict 1..n with nil holes) → its non-nil values."""
    if not tbl:
        return []
    if isinstance(tbl, dict):
        return [tbl[k] for k in sorted(k for k in tbl if isinstance(k, int))]
    return [tbl]
