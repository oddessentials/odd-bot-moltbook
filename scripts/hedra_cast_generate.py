#!/usr/bin/env python3
"""One-shot cast image generation via Hedra text-to-image.

Generates the podcast cast portraits (Shrimp anchor + Carl crab guest) using
Imagen4 with a shared style spec, downloads the bytes locally for our own
backup, and records the resulting Hedra asset IDs to stdout for inclusion in
config/podcast-cast.yaml.

This is one-time cast establishment. The recurring engine never re-runs this.
If Hedra evicts an asset later, we re-run this to rebuild the cast and update
the cast contract.

Outputs:
  - data/cast/shrimp.png + data/cast/carl.png (gitignored backup bytes)
  - Stdout: asset_id for each character (paste into config/podcast-cast.yaml)
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
KEYS_FILE = REPO_ROOT / ".keys"
CAST_DIR = REPO_ROOT / "data" / "cast"

API_BASE = "https://api.hedra.com/web-app/public"
IMAGEN4_MODEL_ID = "18fd3aa1-a64c-44f6-a5a4-ed1ec831c72b"

STYLE_BASE = (
    "Children's book illustration style, vibrant flat colors, clean line art, "
    "friendly expression, plain off-white studio backdrop, head-and-shoulders "
    "portrait, looking straight at camera, eye contact, mouth slightly parted "
    "as if mid-sentence."
)
PROMPTS = {
    "shrimp": (
        "An anthropomorphic pink-orange cartoon shrimp character named Shrimp, "
        "small, witty, energetic. Two large round expressive eyes on stalks, a "
        "small mouth, antennae arching back. Wears a tiny white t-shirt. "
        + STYLE_BASE
    ),
    "carl": (
        "An anthropomorphic red cartoon crab character named Carl, sardonic but "
        "friendly. Round red crab head and shoulders only, no claws or body in "
        "frame, two large round friendly eyes, expressive eyebrows, a wide "
        "smiling mouth with visible lips. Wears a small striped scarf around "
        "the neck. " + STYLE_BASE
    ),
}

ASPECT_RATIO = "1:1"
RESOLUTION = "1K"


def load_api_key() -> str:
    text = KEYS_FILE.read_text()
    m = re.search(r"^Hedra Key:\s*(\S+)", text, flags=re.MULTILINE | re.IGNORECASE)
    if not m:
        raise SystemExit("Hedra key not found in .keys.")
    return m.group(1).strip()


def hedra_session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.headers["x-api-key"] = api_key
    return s


def generate(s: requests.Session, slug: str, prompt: str) -> tuple[str, str]:
    body = {
        "type": "image",
        "ai_model_id": IMAGEN4_MODEL_ID,
        "text_prompt": prompt,
        "aspect_ratio": ASPECT_RATIO,
        "resolution": RESOLUTION,
    }
    print(f"[{slug}] submitting Imagen4 generation...")
    resp = s.post(f"{API_BASE}/generations", json=body, timeout=60)
    if not resp.ok:
        raise SystemExit(f"[{slug}] generation POST failed: {resp.status_code} {resp.text}")
    gen = resp.json()
    gen_id = gen["id"]
    asset_id = gen.get("asset_id")
    print(f"[{slug}] generation_id={gen_id} asset_id={asset_id}")

    t0 = time.time()
    last_status = None
    download_url = None
    while True:
        st = s.get(f"{API_BASE}/generations/{gen_id}/status", timeout=30)
        st.raise_for_status()
        data = st.json()
        status = data.get("status")
        if status != last_status:
            print(f"[{slug}]   [{time.time() - t0:5.1f}s] status={status} progress={data.get('progress')}")
            last_status = status
        if status == "complete":
            download_url = data.get("url") or data.get("download_url")
            asset_id = data.get("asset_id") or asset_id
            break
        if status == "error":
            raise SystemExit(f"[{slug}] errored: {json.dumps(data, indent=2)}")
        time.sleep(3)

    if not download_url:
        # For T2I, the generation status response leaves url/download_url null.
        # Fetch the signed image URL from the asset record instead.
        asset_resp = s.get(
            f"{API_BASE}/assets",
            params={"type": "image", "ids": asset_id},
            timeout=30,
        )
        asset_resp.raise_for_status()
        items = asset_resp.json()
        if not items or not items[0].get("asset", {}).get("url"):
            raise SystemExit(f"[{slug}] no asset url: {items}")
        download_url = items[0]["asset"]["url"]
    print(f"[{slug}] downloading {download_url[:80]}...")
    with requests.get(download_url, stream=True, timeout=120) as r:
        r.raise_for_status()
        CAST_DIR.mkdir(parents=True, exist_ok=True)
        out = CAST_DIR / f"{slug}.png"
        with out.open("wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    print(f"[{slug}] wrote {out.stat().st_size} bytes to {out}")
    return asset_id, str(out)


def main() -> int:
    args = sys.argv[1:]
    targets = args if args else list(PROMPTS.keys())
    unknown = [t for t in targets if t not in PROMPTS]
    if unknown:
        raise SystemExit(f"Unknown cast slug(s): {unknown}. Known: {list(PROMPTS.keys())}")

    api_key = load_api_key()
    s = hedra_session(api_key)

    results = {}
    for slug in targets:
        asset_id, local_path = generate(s, slug, PROMPTS[slug])
        results[slug] = {"asset_id": asset_id, "local_path": local_path}

    print()
    print("=== CAST ASSET IDS (paste into config/podcast-cast.yaml) ===")
    for slug, info in results.items():
        print(f"  {slug}: {info['asset_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
