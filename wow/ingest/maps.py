"""Zone map art for the quest guide route map: a one-time extract, re-runnable.

    python -m ingest.maps            # latest Classic Era build; cached tiles reused
    python -m ingest.maps --force    # re-download tiles

Classic Era art, not Forever's: route points are QuestieDB's Classic zone
coordinates, and Forever redrew every zone map (all 49 zones' tile sets differ
in 1.60.1.69977), so Classic coords only line up with Classic art. The six
new Forever zones (Mount Hyjal, Darkspear Islands, …) have art in the Forever
client but no quest coordinates yet, so they're left for later.

Source: the client itself, via wago.tools (same as every other table here).
UiMap (Type 3 = zone) → UiMapXMapArt → UiMapArtTile lists each zone's 4×3
grid of 256px BLP tiles by FileDataID; they're fetched once from wago.tools'
file endpoint, cached as data/raw/maps/<build>/<fdid>.blp, decoded with
Pillow, stitched, and cropped to UiMapArtStyleLayer's 1002×668 visible
layer. Classic maps draw towns, lakes and roads as "explored area" overlays
(WorldMapOverlay → WorldMapOverlayTile, alpha BLPs at a pixel offset), so
every overlay is composited on top: the fully explored map. That layer is exactly the 0–1 (QuestieDB: 0–100) space of the zone's
UiMap, so coordinates need no per-zone calibration.

Output: data/maps/<uiMapId>.png + data/maps/index.json. Nothing in the daily
ingest touches these; the app serves them from /maps/.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image

from common.constants import CLASSIC_ERA_PRODUCT

from . import wago_client
from .wago_client import read_csv

log = logging.getLogger("wow.ingest")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MAPS_DIR = DATA_DIR / "maps"
CACHE_DIR = DATA_DIR / "raw"
TABLES = ("UiMap", "UiMapXMapArt", "UiMapArt", "UiMapArtTile", "UiMapArtStyleLayer", "UiMapAssignment",
          "WorldMapOverlay", "WorldMapOverlayTile")
FILE_URL = "https://wago.tools/api/casc/{fdid}?version={build}"
UIMAP_ZONE = "3"
DOWNLOAD_PAUSE = 0.25  # seconds between tile requests; this is a one-time pull, no hurry


def zone_maps(build_dir: Path) -> list[dict]:
    """Each zone UiMap with its AreaTable id, art layer size and tile grid."""
    zones = {int(r["ID"]): r["Name_lang"] for r in read_csv(build_dir / "UiMap.csv") if r["Type"] == UIMAP_ZONE}
    area = {}
    for r in read_csv(build_dir / "UiMapAssignment.csv"):
        if int(r["UiMapID"]) in zones and int(r["AreaID"]):
            area.setdefault(int(r["UiMapID"]), int(r["AreaID"]))
    art = {int(r["UiMapID"]): int(r["UiMapArtID"]) for r in read_csv(build_dir / "UiMapXMapArt.csv")
           if int(r["PhaseID"] or 0) == 0}
    style = {int(r["ID"]): int(r["UiMapArtStyleID"]) for r in read_csv(build_dir / "UiMapArt.csv")}
    layers = {int(r["UiMapArtStyleID"]): r for r in read_csv(build_dir / "UiMapArtStyleLayer.csv")
              if int(r["LayerIndex"]) == 0}
    tiles: dict[int, list[tuple[int, int, int]]] = {}
    for r in read_csv(build_dir / "UiMapArtTile.csv"):
        if int(r["LayerIndex"]) == 0:
            tiles.setdefault(int(r["UiMapArtID"]), []).append((int(r["RowIndex"]), int(r["ColIndex"]), int(r["FileDataID"])))
    overlay_tiles: dict[int, list[tuple[int, int, int]]] = {}
    for r in read_csv(build_dir / "WorldMapOverlayTile.csv"):
        if int(r["LayerIndex"]) == 0:
            overlay_tiles.setdefault(int(r["WorldMapOverlayID"]), []).append(
                (int(r["RowIndex"]), int(r["ColIndex"]), int(r["FileDataID"])))
    overlays: dict[int, list[dict]] = {}
    for r in read_csv(build_dir / "WorldMapOverlay.csv"):
        if int(r["PlayerConditionID"] or 0) == 0 and overlay_tiles.get(int(r["ID"])):
            overlays.setdefault(int(r["UiMapArtID"]), []).append({
                "x": int(r["OffsetX"]), "y": int(r["OffsetY"]), "w": int(r["TextureWidth"]),
                "h": int(r["TextureHeight"]), "tiles": sorted(overlay_tiles[int(r["ID"])])})
    out = []
    for uid, name in sorted(zones.items()):
        art_id = art.get(uid)
        layer = layers.get(style.get(art_id))
        if not art_id or not layer or not tiles.get(art_id):
            log.warning("map %s (%s): no art in this build", uid, name)
            continue
        out.append({"ui_map": uid, "name": name, "area_id": area.get(uid), "tiles": sorted(tiles[art_id]),
                    "overlays": overlays.get(art_id, []),
                    "width": int(layer["LayerWidth"]), "height": int(layer["LayerHeight"]),
                    "tile_w": int(layer["TileWidth"]), "tile_h": int(layer["TileHeight"])})
    return out


def _tile(fdid: int, build: str, cache: Path, force: bool) -> Image.Image:
    """One BLP tile as RGBA (overlays carry alpha; base tiles are opaque)."""
    path = cache / f"{fdid}.blp"
    if force or not path.exists():
        for attempt in range(3):
            try:
                resp = requests.get(FILE_URL.format(fdid=fdid, build=build), timeout=60,
                                    headers={"User-Agent": "augustserver.com wow-maps (one-time map extract)"})
                resp.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(3 * (attempt + 1))
        if not resp.content.startswith(b"BLP"):
            raise RuntimeError(f"file {fdid} isn't a BLP ({resp.content[:8]!r})")
        path.write_bytes(resp.content)
        time.sleep(DOWNLOAD_PAUSE)
    return Image.open(BytesIO(path.read_bytes())).convert("RGBA")


def build_maps(build: str, force: bool = False) -> dict:
    for table in TABLES:
        wago_client.download_csv(table, build, CACHE_DIR, force=force)
    cache = CACHE_DIR / "maps" / build
    cache.mkdir(parents=True, exist_ok=True)
    MAPS_DIR.mkdir(parents=True, exist_ok=True)
    index = {}
    for z in zone_maps(CACHE_DIR / build):
        rows = max(r for r, _, _ in z["tiles"]) + 1
        cols = max(c for _, c, _ in z["tiles"]) + 1
        canvas = Image.new("RGBA", (cols * z["tile_w"], rows * z["tile_h"]))
        for r, c, fdid in z["tiles"]:
            canvas.paste(_tile(fdid, build, cache, force), (c * z["tile_w"], r * z["tile_h"]))
        for ov in z["overlays"]:
            # Overlay tiles are 256px (the last row/column may be a smaller texture);
            # the overlay as a whole is clipped to its TextureWidth × TextureHeight.
            layer = Image.new("RGBA", (ov["w"], ov["h"]))
            for r, c, fdid in ov["tiles"]:
                layer.paste(_tile(fdid, build, cache, force), (c * 256, r * 256))
            canvas.alpha_composite(layer, (ov["x"], ov["y"]))
        img = canvas.crop((0, 0, z["width"], z["height"])).convert("RGB")
        img.save(MAPS_DIR / f"{z['ui_map']}.png", optimize=True)
        index[z["ui_map"]] = {"name": z["name"], "area_id": z["area_id"], "width": z["width"], "height": z["height"]}
        log.info("map %s %s: %dx%d, %d tiles + %d overlays", z["ui_map"], z["name"], z["width"], z["height"],
                 len(z["tiles"]), len(z["overlays"]))
    (MAPS_DIR / "index.json").write_text(json.dumps({"build": build, "maps": index}, indent=1, sort_keys=True))
    return index


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--build", help="Classic Era build version (default: latest)")
    parser.add_argument("--force", action="store_true", help="re-download cached tiles and tables")
    args = parser.parse_args()
    build = args.build or wago_client.latest_build(CLASSIC_ERA_PRODUCT)["version"]
    log.info("Zone maps from Classic Era %s", build)
    index = build_maps(build, force=args.force)
    log.info("Wrote %d zone maps to %s", len(index), MAPS_DIR)


if __name__ == "__main__":
    main()
