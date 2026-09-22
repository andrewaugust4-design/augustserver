"""Item/DB2 enum values and slot layout, verified 2026-09-22 against live
wago.tools CSV exports for both the WoW: Forever beta (product wow_classic_beta,
build 1.60.1.69913, build_config description "WOW-69913patch1.60.1_ForeverBeta")
and Classic Era (product wow_classic_era, build 1.15.9.69722). Do not hand-edit
without re-checking a fresh CSV — these are the raw Blizzard DB2 enum values,
not display labels.
"""

# ── wago.tools products ─────────────────────────────────────────────────────
FOREVER_PRODUCT = "wow_classic_beta"
CLASSIC_ERA_PRODUCT = "wow_classic_era"

# ── Item.ClassID ────────────────────────────────────────────────────────────
ITEM_CLASS_WEAPON = 2
ITEM_CLASS_ARMOR = 4

# ── Item.SubclassID for ClassID=2 (Weapon) ──────────────────────────────────
WEAPON_AXE1H = 0
WEAPON_AXE2H = 1
WEAPON_BOW = 2
WEAPON_GUN = 3
WEAPON_MACE1H = 4
WEAPON_MACE2H = 5
WEAPON_POLEARM = 6
WEAPON_SWORD1H = 7
WEAPON_SWORD2H = 8
WEAPON_STAFF = 10
WEAPON_FIST = 13
WEAPON_DAGGER = 15
WEAPON_THROWN = 16
WEAPON_CROSSBOW = 18
WEAPON_WAND = 19
# Not real player-facing weapon types (unused/obsolete/debug) — excluded from
# the gear browser entirely: 9, 11, 12 (obsolete), 14 (Misc, e.g. quest fists),
# 17 (Spear — vestigial, no live items use it), 20 (Fishing Pole).

WEAPON_SUBCLASS_NAMES = {
    WEAPON_AXE1H: "One-Handed Axe",
    WEAPON_AXE2H: "Two-Handed Axe",
    WEAPON_BOW: "Bow",
    WEAPON_GUN: "Gun",
    WEAPON_MACE1H: "One-Handed Mace",
    WEAPON_MACE2H: "Two-Handed Mace",
    WEAPON_POLEARM: "Polearm",
    WEAPON_SWORD1H: "One-Handed Sword",
    WEAPON_SWORD2H: "Two-Handed Sword",
    WEAPON_STAFF: "Staff",
    WEAPON_FIST: "Fist Weapon",
    WEAPON_DAGGER: "Dagger",
    WEAPON_THROWN: "Thrown",
    WEAPON_CROSSBOW: "Crossbow",
    WEAPON_WAND: "Wand",
}

# ── Item.SubclassID for ClassID=4 (Armor) ───────────────────────────────────
ARMOR_MISC = 0       # rings, necks, cloaks, trinkets, shirts, tabards — unrestricted
ARMOR_CLOTH = 1
ARMOR_LEATHER = 2
ARMOR_MAIL = 3
ARMOR_PLATE = 4
ARMOR_SHIELD = 6
ARMOR_LIBRAM = 7     # Paladin relic
ARMOR_IDOL = 8       # Druid relic
ARMOR_TOTEM = 9      # Shaman relic

ARMOR_MATERIAL_NAMES = {
    ARMOR_CLOTH: "Cloth",
    ARMOR_LEATHER: "Leather",
    ARMOR_MAIL: "Mail",
    ARMOR_PLATE: "Plate",
    ARMOR_SHIELD: "Shield",
}
ARMOR_MATERIAL_RANK = {ARMOR_CLOTH: 1, ARMOR_LEATHER: 2, ARMOR_MAIL: 3, ARMOR_PLATE: 4}

RELIC_SUBCLASS_CLASS = {ARMOR_LIBRAM: "paladin", ARMOR_IDOL: "druid", ARMOR_TOTEM: "shaman"}

# ── Item.InventoryType -> paperdoll slot bucket ─────────────────────────────
# Two-handers (17) fold into "mainhand" (classic UI equips them in the main
# hand slot and blocks off-hand). Wands/guns/crossbows share invtype 26 with
# bows split out as 15; all three fold into "ranged" for browsing purposes.
SLOTS = [
    {"slug": "head", "label": "Head", "invtypes": [1]},
    {"slug": "neck", "label": "Neck", "invtypes": [2]},
    {"slug": "shoulder", "label": "Shoulder", "invtypes": [3]},
    {"slug": "back", "label": "Back", "invtypes": [16]},
    {"slug": "chest", "label": "Chest", "invtypes": [5, 20]},
    {"slug": "wrist", "label": "Wrist", "invtypes": [9]},
    {"slug": "hands", "label": "Hands", "invtypes": [10]},
    {"slug": "waist", "label": "Waist", "invtypes": [6]},
    {"slug": "legs", "label": "Legs", "invtypes": [7]},
    {"slug": "feet", "label": "Feet", "invtypes": [8]},
    {"slug": "finger", "label": "Ring", "invtypes": [11]},
    {"slug": "trinket", "label": "Trinket", "invtypes": [12]},
    {"slug": "relic", "label": "Relic", "invtypes": [28]},
    {"slug": "mainhand", "label": "Main Hand", "invtypes": [13, 21, 17]},
    {"slug": "offhand", "label": "Off Hand", "invtypes": [22, 23, 14]},
    {"slug": "ranged", "label": "Ranged", "invtypes": [15, 25, 26]},
]
INVTYPE_TO_SLOT = {inv: s["slug"] for s in SLOTS for inv in s["invtypes"]}
SLOT_BY_SLUG = {s["slug"]: s for s in SLOTS}

# ── Classes ──────────────────────────────────────────────────────────────
# Blizzard classId (used for the AllowableClass bitmask, bit = 1 << (classId-1))
# alongside our own slug + display color for the frontend.
CLASSES = [
    {"slug": "warrior", "name": "Warrior", "class_id": 1, "color": "#C79C6E"},
    {"slug": "paladin", "name": "Paladin", "class_id": 2, "color": "#F58CBA"},
    {"slug": "hunter", "name": "Hunter", "class_id": 3, "color": "#ABD473"},
    {"slug": "rogue", "name": "Rogue", "class_id": 4, "color": "#FFF569"},
    {"slug": "priest", "name": "Priest", "class_id": 5, "color": "#FFFFFF"},
    {"slug": "shaman", "name": "Shaman", "class_id": 7, "color": "#0070DE"},
    {"slug": "mage", "name": "Mage", "class_id": 8, "color": "#40C7EB"},
    {"slug": "warlock", "name": "Warlock", "class_id": 9, "color": "#8787ED"},
    {"slug": "druid", "name": "Druid", "class_id": 11, "color": "#FF7D0A"},
]
CLASS_BY_SLUG = {c["slug"]: c for c in CLASSES}
CLASS_BIT = {c["slug"]: 1 << (c["class_id"] - 1) for c in CLASSES}
