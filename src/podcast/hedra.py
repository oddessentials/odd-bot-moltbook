"""Hedra Character-3 clip generation: upload audio, submit, poll, download.

The image asset_id comes from the cast contract (already in Hedra storage,
established out-of-band). Only the per-segment audio asset is uploaded
fresh on each call; clip generation references both by id.
"""

from __future__ import annotations

import time
from pathlib import Path

import requests

from .config import (
    ASPECT_RATIO,
    HEDRA_API_BASE,
    HEDRA_MODEL_ID,
    RESOLUTION,
)


def hedra_session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.headers["x-api-key"] = api_key
    return s


def upload_hedra_audio(s: requests.Session, audio_path: Path) -> str:
    create = s.post(
        f"{HEDRA_API_BASE}/assets",
        json={"name": audio_path.name, "type": "audio"},
        timeout=30,
    )
    create.raise_for_status()
    asset_id = create.json()["id"]
    with audio_path.open("rb") as f:
        up = s.post(f"{HEDRA_API_BASE}/assets/{asset_id}/upload", files={"file": f}, timeout=300)
    up.raise_for_status()
    return asset_id


def submit_hedra_clip(
    s: requests.Session,
    *,
    image_asset_id: str,
    audio_asset_id: str,
    text_prompt: str,
) -> str:
    body = {
        "type": "video",
        "ai_model_id": HEDRA_MODEL_ID,
        "start_keyframe_id": image_asset_id,
        "audio_id": audio_asset_id,
        "generated_video_inputs": {
            "text_prompt": text_prompt,
            "resolution": RESOLUTION,
            "aspect_ratio": ASPECT_RATIO,
        },
    }
    resp = s.post(f"{HEDRA_API_BASE}/generations", json=body, timeout=60)
    resp.raise_for_status()
    return resp.json()["id"]


def poll_hedra_clip(
    s: requests.Session,
    gen_id: str,
    *,
    poll_interval_sec: float = 5.0,
) -> tuple[str, str]:
    """Block until the Hedra generation is complete or errored.

    Returns (clip_asset_id, download_url). Raises RuntimeError on error.
    """
    while True:
        st = s.get(f"{HEDRA_API_BASE}/generations/{gen_id}/status", timeout=30)
        st.raise_for_status()
        data = st.json()
        status = data.get("status")
        if status == "complete":
            url = data.get("url") or data.get("download_url")
            asset_id = data.get("asset_id")
            if not url or not asset_id:
                raise RuntimeError(f"complete with missing url/asset_id: {data}")
            return asset_id, url
        if status == "error":
            raise RuntimeError(f"Hedra generation {gen_id} errored: {data}")
        time.sleep(poll_interval_sec)


def download_clip(url: str, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with out_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    return out_path
