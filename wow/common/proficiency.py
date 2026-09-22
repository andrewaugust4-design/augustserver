"""Classic-era weapon/armor proficiency matrix.

Encoded from Warcraft Tavern's per-class Classic guides (weapon skills +
armor sections), cross-checked class by class on 2026-09-22 rather than
pulled from memory:
  warcrafttavern.com/wow-classic/guides/{warrior,paladin,hunter,rogue,
  priest,mage,warlock,shaman,druid}/

"Equippable" here means *ever* equippable — level/quest-gated unlocks
(Warrior/Paladin plate at 40, Hunter/Shaman mail at 40, Paladin/Warrior
polearm and Hunter polearm at 20) are included, matching how the gear
browser is meant to work ("what can this class eventually wear").

Forever is a new Classic+ branch; if new race/class combos or a datamined
skill change ever contradicts one of these lines, correct it here — this
table intentionally baselines on standard Classic and is the single place
class-equip logic lives (imported by both the ingest diff step, for
category bookkeeping, and the API's per-class item filter).
"""

from .constants import (
    ARMOR_CLOTH,
    ARMOR_LEATHER,
    ARMOR_MAIL,
    ARMOR_MATERIAL_RANK,
    ARMOR_MISC,
    ARMOR_PLATE,
    ARMOR_SHIELD,
    CLASS_BIT,
    ITEM_CLASS_ARMOR,
    ITEM_CLASS_WEAPON,
    RELIC_SUBCLASS_CLASS,
    WEAPON_AXE1H,
    WEAPON_AXE2H,
    WEAPON_BOW,
    WEAPON_CROSSBOW,
    WEAPON_DAGGER,
    WEAPON_FIST,
    WEAPON_GUN,
    WEAPON_MACE1H,
    WEAPON_MACE2H,
    WEAPON_POLEARM,
    WEAPON_STAFF,
    WEAPON_SWORD1H,
    WEAPON_SWORD2H,
    WEAPON_THROWN,
    WEAPON_WAND,
)

# Max armor material each class can ever wear. Classes not listed here can't
# wear cloth/leather/mail/plate at all (not a real case in Classic, kept for
# completeness / future Forever-only classes).
CLASS_ARMOR_MAX = {
    "warrior": ARMOR_PLATE,
    "paladin": ARMOR_PLATE,
    "hunter": ARMOR_MAIL,
    "shaman": ARMOR_MAIL,
    "rogue": ARMOR_LEATHER,
    "druid": ARMOR_LEATHER,
    "priest": ARMOR_CLOTH,
    "mage": ARMOR_CLOTH,
    "warlock": ARMOR_CLOTH,
}

SHIELD_CLASSES = {"warrior", "paladin", "shaman"}

# Classes that can wield a one-hander in the off hand (vanilla: Warrior and
# Hunter via the Dual Wield skill, Rogue baseline). Shamans only got dual
# wield in TBC. Used by the set builder to allow One-Hand items in off hand.
DUAL_WIELD_CLASSES = {"warrior", "rogue", "hunter"}

# Full weapon-type matrix, verified per class (see module docstring).
CLASS_WEAPONS = {
    "warrior": {
        WEAPON_AXE1H, WEAPON_AXE2H, WEAPON_BOW, WEAPON_CROSSBOW, WEAPON_DAGGER,
        WEAPON_FIST, WEAPON_GUN, WEAPON_MACE1H, WEAPON_MACE2H, WEAPON_POLEARM,
        WEAPON_STAFF, WEAPON_SWORD1H, WEAPON_SWORD2H, WEAPON_THROWN,
    },  # everything except Wand
    "paladin": {
        WEAPON_AXE1H, WEAPON_AXE2H, WEAPON_MACE1H, WEAPON_MACE2H,
        WEAPON_POLEARM, WEAPON_SWORD1H, WEAPON_SWORD2H,
    },
    "hunter": {
        WEAPON_AXE1H, WEAPON_AXE2H, WEAPON_BOW, WEAPON_CROSSBOW, WEAPON_DAGGER,
        WEAPON_FIST, WEAPON_GUN, WEAPON_POLEARM, WEAPON_STAFF, WEAPON_SWORD1H,
        WEAPON_SWORD2H,
    },  # everything except maces, thrown, wands
    "rogue": {
        WEAPON_BOW, WEAPON_CROSSBOW, WEAPON_DAGGER, WEAPON_FIST, WEAPON_GUN,
        WEAPON_MACE1H, WEAPON_SWORD1H, WEAPON_THROWN,
    },  # no axes, no polearm/staff, no two-handers, no wand
    "priest": {WEAPON_DAGGER, WEAPON_MACE1H, WEAPON_STAFF, WEAPON_WAND},
    "mage": {WEAPON_DAGGER, WEAPON_STAFF, WEAPON_SWORD1H, WEAPON_WAND},
    "warlock": {WEAPON_DAGGER, WEAPON_STAFF, WEAPON_SWORD1H, WEAPON_WAND},
    "shaman": {
        WEAPON_AXE1H, WEAPON_AXE2H, WEAPON_DAGGER, WEAPON_FIST,
        WEAPON_MACE1H, WEAPON_MACE2H, WEAPON_STAFF,
    },  # no swords, no polearm, no ranged, no wand
    "druid": {WEAPON_DAGGER, WEAPON_FIST, WEAPON_MACE1H, WEAPON_MACE2H, WEAPON_STAFF},
}


# Some items encode "no restriction" as -1; others spell it out as a mask
# with every class bit set (commonly 32767 = bits 0-14, a leftover from a
# wider class roster than Classic ships) instead. Either form means the same
# thing, so a mask only counts as a *real* restriction if it excludes at
# least one of the classes we track.
ALL_CLASS_BITS = 0
for _bit in CLASS_BIT.values():
    ALL_CLASS_BITS |= _bit


def can_equip(class_slug: str, item_class: int, item_subclass: int, allowable_class_mask) -> bool:
    """Whether `class_slug` can ever equip an item with this class/subclass,
    honoring an explicit AllowableClass bitmask when the item has one that
    actually restricts at least one Classic class, falling back to the
    proficiency table otherwise.
    """
    if allowable_class_mask is not None:
        mask = int(allowable_class_mask)
        if mask != -1 and (mask & ALL_CLASS_BITS) != ALL_CLASS_BITS:
            return bool(mask & CLASS_BIT[class_slug])

    if item_class == ITEM_CLASS_ARMOR:
        if item_subclass == ARMOR_MISC:
            return True
        if item_subclass == ARMOR_SHIELD:
            return class_slug in SHIELD_CLASSES
        if item_subclass in RELIC_SUBCLASS_CLASS:
            return RELIC_SUBCLASS_CLASS[item_subclass] == class_slug
        material_rank = ARMOR_MATERIAL_RANK.get(item_subclass)
        if material_rank is None:
            return True  # unrecognized armor subclass — don't block browsing
        max_material = CLASS_ARMOR_MAX.get(class_slug)
        if max_material is None:
            return False
        return material_rank <= ARMOR_MATERIAL_RANK[max_material]

    if item_class == ITEM_CLASS_WEAPON:
        return item_subclass in CLASS_WEAPONS.get(class_slug, set())

    return True
