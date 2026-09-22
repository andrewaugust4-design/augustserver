"""Forever base armor: computed from the armor DB2 tables, like stats are
from RandPropPoints (common/stats.py). Forever doesn't store it per item;
Classic Era does (ItemSparse.Resistances_0), which is what ingest/validate.py
checks this against on every ingest.

    armor = round(ItemArmorTotal[ilvl][material]
                  × ArmorLocation[invType][material column]
                  × ItemArmorQuality[ilvl][Qualitymod_<quality>])
    shield armor = ItemArmorShield[ilvl][Quality_<quality>]   (no location factor)

Fitted 2026-09-22 against Classic Era (Forever 1.60.1.69913 tables, which
are byte-identical to Era 1.15.9.69722's): 99.25% of `unchanged` armor items
match exactly (1989/2004). Details that the fit locked in:
- Round to nearest (floor matches only ~52%).
- Robes (InventoryType 20) have an all-zero ArmorLocation row and use the
  Chest row (5) instead: that alone was worth +3%.
- Cloaks are Cloth subclass in the data, so they take the Cloth column.
  ArmorLocation's generic `Modifier` column is NOT used: the only
  checkable misc-subclass cloak (16034, a PvP test cloak) has no armor in
  Classic, and the generic column would have given it 37. The other 7
  misc/cosmetic-subclass cloaks get no armor line.
Misses are hand-set armor on misc items (rings/necks/off-hands/totems like
Emerald Circle), which Forever doesn't express through these tables, plus a
handful of deprecated/test items.
"""

from __future__ import annotations

from .constants import ARMOR_CLOTH, ARMOR_LEATHER, ARMOR_MAIL, ARMOR_PLATE, ARMOR_SHIELD

ARMOR_TABLES = ("ItemArmorTotal", "ItemArmorQuality", "ArmorLocation", "ItemArmorShield")

INVTYPE_CHEST = 5
INVTYPE_ROBE = 20

# ItemArmorTotal column, ArmorLocation column
MATERIAL_COLUMNS = {
    ARMOR_CLOTH: ("Cloth", "Clothmodifier"),
    ARMOR_LEATHER: ("Leather", "Leathermodifier"),
    ARMOR_MAIL: ("Mail", "Chainmodifier"),
    ARMOR_PLATE: ("Plate", "Platemodifier"),
}


def item_armor(tables: dict[str, dict[int, dict]], *, quality: int, item_level: int,
               inventory_type: int, item_subclass: int) -> int | None:
    """Base armor for an armor-class item, or None if it has none (necks,
    rings, trinkets, relics, off-hand frills…) or the tables don't cover it."""
    if item_subclass == ARMOR_SHIELD:
        row = tables["shield"].get(item_level)
        value = int(row[f"Quality_{quality}"]) if row else 0
        return value or None

    if item_subclass not in MATERIAL_COLUMNS:
        return None
    total_col, loc_col = MATERIAL_COLUMNS[item_subclass]

    loc = tables["location"].get(INVTYPE_CHEST if inventory_type == INVTYPE_ROBE else inventory_type)
    total = tables["total"].get(item_level)
    qmod = tables["quality"].get(item_level)
    if not (loc and total and qmod):
        return None
    value = round(float(total[total_col]) * float(loc[loc_col]) * float(qmod[f"Qualitymod_{quality}"]))
    return value or None
