#!/usr/bin/env python3
"""Hedra Character-3 auth + clip generation smoke test.

Verifies the staged Hedra API key, uploads a test image + audio, submits a
Character-3 generation, polls to completion, and downloads the resulting MP4.
Logs queue/process timing so we can sanity-check Creator-tier render speed.

Inputs:
  - data/_smoke/test_portrait.jpg (any portrait image — placeholder for cast)
  - data/_smoke/elevenlabs.mp3    (output of elevenlabs_smoke.py)

Outputs:
  - data/_smoke/hedra.mp4
  - Stdout: model_id used, asset IDs, status timeline, final URL.
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
SMOKE_DIR = REPO_ROOT / "data" / "_smoke"
IMAGE_FILE = SMOKE_DIR / "test_portrait.jpg"
AUDIO_FILE = SMOKE_DIR / "elevenlabs.mp3"
OUT_FILE = SMOKE_DIR / "hedra.mp4"

API_BASE = "https://api.hedra.com/web-app/public"
CHARACTER_3_MODEL_ID = "d1dd37a3-e39a-4854-a298-6510289f9cf2"

# Smoke-test render params: 540p × 16:9 minimizes credit spend (~6 credits/sec).
RESOLUTION = "540p"
ASPECT_RATIO = "16:9"
TEXT_PROMPT = "A friendly podcast host speaking calmly to the camera."


def load_api_key() -> str:
    text = KEYS_FILE.read_text()
    m = re.search(r"^Hedra Key:\s*(\S+)", text, flags=re.MULTILINE | re.IGNORECASE)
    if not m:
        raise SystemExit("Hedra key not found in .keys (looking for 'Hedra Key:' line).")
    return m.group(1).strip()


def hedra_session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.headers["x-api-key"] = api_key
    return s


def upload_asset(s: requests.Session, path: Path, asset_type: str) -> str:
    create = s.post(f"{API_BASE}/assets", json={"name": path.name, "type": asset_type}, timeout=30)
    create.raise_for_status()
    asset_id = create.json()["id"]
    with path.open("rb") as f:
        up = s.post(f"{API_BASE}/assets/{asset_id}/upload", files={"file": f}, timeout=300)
    up.raise_for_status()
    print(f"  uploaded {asset_type} {path.name} -> {asset_id}")
    return asset_id


def main() -> int:
    if not IMAGE_FILE.exists():
        raise SystemExit(f"Missing {IMAGE_FILE}")
    if not AUDIO_FILE.exists():
        raise SystemExit(f"Missing {AUDIO_FILE} — run scripts/elevenlabs_smoke.py first")

    api_key = load_api_key()
    s = hedra_session(api_key)

    # Auth check via /models
    print("Listing available models...")
    models_resp = s.get(f"{API_BASE}/models", timeout=30)
    if not models_resp.ok:
        raise SystemExit(f"models GET failed: {models_resp.status_code} {models_resp.text}")
    models = models_resp.json()
    print(f"  {len(models)} model(s) available:")
    for m in models:
        print(f"    - {m.get('id')} {m.get('name')!r}")
    has_char3 = any(m.get("id") == CHARACTER_3_MODEL_ID for m in models)
    print(f"  Character-3 ({CHARACTER_3_MODEL_ID}) accessible: {has_char3}")

    print("Uploading assets...")
    image_id = upload_asset(s, IMAGE_FILE, "image")
    audio_id = upload_asset(s, AUDIO_FILE, "audio")

    body = {
        "type": "video",
        "ai_model_id": CHARACTER_3_MODEL_ID,
        "start_keyframe_id": image_id,
        "audio_id": audio_id,
        "generated_video_inputs": {
            "text_prompt": TEXT_PROMPT,
            "resolution": RESOLUTION,
            "aspect_ratio": ASPECT_RATIO,
        },
    }
    print(f"Submitting generation: resolution={RESOLUTION} aspect={ASPECT_RATIO}...")
    gen_resp = s.post(f"{API_BASE}/generations", json=body, timeout=60)
    if not gen_resp.ok:
        raise SystemExit(f"generation POST failed: {gen_resp.status_code} {gen_resp.text}")
    gen = gen_resp.json()
    print(f"  generation accepted: {json.dumps(gen, indent=2)}")
    gen_id = gen["id"]

    print("Polling status...")
    t0 = time.time()
    last_status = None
    download_url = None
    while True:
        status_resp = s.get(f"{API_BASE}/generations/{gen_id}/status", timeout=30)
        status_resp.raise_for_status()
        st = status_resp.json()
        status = st.get("status")
        progress = st.get("progress")
        eta = st.get("eta_sec")
        if status != last_status:
            elapsed = time.time() - t0
            print(f"  [{elapsed:6.1f}s] status={status} progress={progress} eta={eta}")
            last_status = status
        if status == "complete":
            download_url = st.get("url") or st.get("download_url")
            asset_id = st.get("asset_id")
            print(f"  asset_id={asset_id}")
            break
        if status == "error":
            raise SystemExit(f"Generation errored: {st}")
        time.sleep(5)

    elapsed_total = time.time() - t0
    print(f"Total wall time to complete: {elapsed_total:.1f}s")

    if not download_url:
        raise SystemExit(f"No download URL on completion: {st}")

    print(f"Downloading {download_url}...")
    with requests.get(download_url, stream=True, timeout=300) as r:
        r.raise_for_status()
        SMOKE_DIR.mkdir(parents=True, exist_ok=True)
        with OUT_FILE.open("wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    size = OUT_FILE.stat().st_size
    print(f"Wrote {size} bytes to {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
