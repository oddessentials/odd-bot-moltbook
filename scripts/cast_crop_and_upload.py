#!/usr/bin/env python3
"""Center-crop the cast PNGs to true 16:9 and re-upload to Hedra as new assets.

Imagen4's "16:9" aspect_ratio preset produces 1408x768 images (aspect 1.833,
not true 16:9 = 1.778). Hedra Character-3 preserves source aspect at render
time, so this delivers 1320x720 output which fails our objective canary gate.

This script:
  - Loads each cast PNG under data/cast/<slug>.png.
  - Center-crops to exact 1280x720 (true 16:9 at 720p resolution).
  - Overwrites the local PNG (gitignored — bytes are local recovery backup).
  - Uploads the cropped image to Hedra as a NEW image asset.
  - Prints the new asset IDs to paste into config/podcast-cast.yaml.

Run only when the cast PNGs were generated at a non-16:9 aspect AND the cast
contract needs new asset IDs as a result. Re-establishment tooling — never
called from the recurring engine.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import requests
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
KEYS_FILE = REPO_ROOT / ".keys"
CAST_DIR = REPO_ROOT / "data" / "cast"
TARGET_W = 1280
TARGET_H = 720
HEDRA_API_BASE = "https://api.hedra.com/web-app/public"

CAST_SLUGS = ["shrimp", "carl"]


def load_hedra_key() -> str:
    text = KEYS_FILE.read_text()
    m = re.search(r"^Hedra Key:\s*(\S+)", text, flags=re.MULTILINE | re.IGNORECASE)
    if not m:
        raise SystemExit("Hedra key not found in .keys.")
    return m.group(1).strip()


def crop_to_target(src: Path, dst: Path) -> tuple[int, int, int, int]:
    img = Image.open(src).convert("RGB")
    sw, sh = img.size
    target_aspect = TARGET_W / TARGET_H
    src_aspect = sw / sh
    if src_aspect > target_aspect:
        # source wider — crop width
        new_w = int(round(sh * target_aspect))
        x0 = (sw - new_w) // 2
        box = (x0, 0, x0 + new_w, sh)
    else:
        # source taller — crop height
        new_h = int(round(sw / target_aspect))
        y0 = (sh - new_h) // 2
        box = (0, y0, sw, y0 + new_h)
    cropped = img.crop(box)
    resized = cropped.resize((TARGET_W, TARGET_H), Image.LANCZOS)
    resized.save(dst, format="PNG", optimize=True)
    return box


def upload(s: requests.Session, path: Path) -> str:
    create = s.post(
        f"{HEDRA_API_BASE}/assets",
        json={"name": path.name, "type": "image"},
        timeout=30,
    )
    create.raise_for_status()
    asset_id = create.json()["id"]
    with path.open("rb") as f:
        up = s.post(f"{HEDRA_API_BASE}/assets/{asset_id}/upload", files={"file": f}, timeout=120)
    up.raise_for_status()
    return asset_id


def main() -> int:
    api_key = load_hedra_key()
    s = requests.Session()
    s.headers["x-api-key"] = api_key

    new_ids: dict[str, str] = {}
    for slug in CAST_SLUGS:
        src = CAST_DIR / f"{slug}.png"
        if not src.exists():
            raise SystemExit(f"missing {src}")
        box = crop_to_target(src, src)
        with Image.open(src) as im:
            print(f"[{slug}] cropped to {im.size} (crop box {box})")
        asset_id = upload(s, src)
        print(f"[{slug}] uploaded -> {asset_id}")
        new_ids[slug] = asset_id

    print()
    print("=== NEW CAST ASSET IDS (paste into config/podcast-cast.yaml) ===")
    for slug, aid in new_ids.items():
        print(f"  {slug}: {aid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
