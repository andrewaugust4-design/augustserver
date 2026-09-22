"""Set-builder loadout: the equipment slots, which items fit each, and the
rules that flag an equipped item (the frontend enforces the same rules at
click time; this is the authority for saved/loaded sets).

A loadout is {class, race, level, slots: {slot: itemId}}. Only ids are
stored; everything else is looked up live, so a saved set picks up data
fixes and an id that vanished from the data shows as "unavailable".
"""

from __future__ import annotations

from .constants import ARMOR_SHIELD, CLASS_BY_SLUG, ITEM_CLASS_ARMOR
from .proficiency import DUAL_WIELD_CLASSES, can_equip

INVTYPE_ONE_HAND = 13
INVTYPE_TWO_HAND = 17

# slot -> (label, paperdoll browse slug, allowed InventoryTypes)
LOADOUT_SLOTS: dict[str, tuple[str, str, frozenset[int]]] = {
    "head": ("Head", "head", frozenset({1})),
    "neck": ("Neck", "neck", frozenset({2})),
    "shoulder": ("Shoulder", "shoulder", frozenset({3})),
    "back": ("Back", "back", frozenset({16})),
    "chest": ("Chest", "chest", frozenset({5, 20})),
    "wrist": ("Wrist", "wrist", frozenset({9})),
    "hands": ("Hands", "hands", frozenset({10})),
    "waist": ("Waist", "waist", frozenset({6})),
    "legs": ("Legs", "legs", frozenset({7})),
    "feet": ("Feet", "feet", frozenset({8})),
    "finger1": ("Ring 1", "finger", frozenset({11})),
    "finger2": ("Ring 2", "finger", frozenset({11})),
    "trinket1": ("Trinket 1", "trinket", frozenset({12})),
    "trinket2": ("Trinket 2", "trinket", frozenset({12})),
    "mainhand": ("Main Hand", "mainhand", frozenset({13, 17, 21})),
    "offhand": ("Off Hand", "offhand", frozenset({14, 22, 23})),  # + One-Hand for dual wielders
    # Vanilla relics (librams/idols/totems) occupy the ranged slot.
    "ranged": ("Ranged / Relic", "ranged", frozenset({15, 25, 26, 28})),
}


def offhand_invtypes(class_slug: str) -> frozenset[int]:
    allowed = LOADOUT_SLOTS["offhand"][2]
    return allowed | {INVTYPE_ONE_HAND} if class_slug in DUAL_WIELD_CLASSES else allowed


def race_allowed(race_mask: int, playable_bit: int | None) -> bool:
    return race_mask == -1 or playable_bit is None or bool(race_mask & (1 << playable_bit))


def is_shield(item: dict) -> bool:
    return item["item_class"] == ITEM_CLASS_ARMOR and item["item_subclass"] == ARMOR_SHIELD


def check(class_slug: str, level: int, playable_bit: int | None,
          slots: dict[str, dict | None]) -> dict[str, list[dict]]:
    """Per-slot problems for a loadout whose ids were already looked up
    (`None` = id not in the data), as {"msg", "blocks"}. Nothing is dropped
    from the loadout. `blocks` marks items that physically can't be worn
    there (so the sheet leaves their stats out). Class, race and level
    problems only warn, so you can plan gear for later levels."""
    issues: dict[str, list[dict]] = {slot: [] for slot in slots}

    def flag(slot: str, msg: str, blocks: bool = False) -> None:
        issues[slot].append({"msg": msg, "blocks": blocks})

    class_name = CLASS_BY_SLUG[class_slug]["name"]
    seen_unique: dict[int, str] = {}
    for slot, item in slots.items():
        if item is None:
            flag(slot, "Item unavailable — it's no longer in the current data.", blocks=True)
            continue
        allowed = offhand_invtypes(class_slug) if slot == "offhand" else LOADOUT_SLOTS[slot][2]
        if item["inventory_type"] not in allowed:
            if slot == "offhand" and item["inventory_type"] == INVTYPE_ONE_HAND:
                flag(slot, f"{class_name}s can't dual wield.", blocks=True)
            else:
                flag(slot, f"Doesn't fit the {LOADOUT_SLOTS[slot][0]} slot.", blocks=True)
        if not can_equip(class_slug, item["item_class"], item["item_subclass"], item["allowable_class_mask"]):
            flag(slot, f"{class_name}s can't use this.")
        if not race_allowed(item["allowable_race_mask"], playable_bit):
            flag(slot, "Your race can't use this.")
        if item["required_level"] > level:
            flag(slot, f"Requires level {item['required_level']}.")
        if item["unique_equipped"]:
            if item["id"] in seen_unique:
                flag(slot, f"Unique — already equipped in {LOADOUT_SLOTS[seen_unique[item['id']]][0]}.", blocks=True)
            else:
                seen_unique[item["id"]] = slot

    main, off = slots.get("mainhand"), slots.get("offhand")
    if main and off and main["inventory_type"] == INVTYPE_TWO_HAND:
        flag("offhand", "Blocked by a two-handed main hand.", blocks=True)
    return {slot: found for slot, found in issues.items() if found}
