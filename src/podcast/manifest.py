"""Episode manifest read/write/update. The manifest is the canonical state
machine for resume across pipeline phases — everything outside
`data/episodes/<id>/` derives from it.

Atomic writes via tempfile + os.replace (POSIX rename(2) atomicity); a
crash mid-write leaves either the previous contents or no file at all,
never a truncated file. A process-local threading.Lock serialises
in-process concurrent updates from the parallel-segment producer.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cast import cast_config_hash
from .config import (
    ASPECT_RATIO,
    DEFAULT_VISIBILITY,
    EPISODES_DIR,
    EPISODES_PUBLIC_PATH,
    HEDRA_MODEL,
    HEDRA_MODEL_ID,
    RESOLUTION,
    SCRIPT_MODEL,
    TTS_MODEL,
)
from .schema import BriefSummary, CastConfig, EpisodeScript


def episode_dir(episode_id: str) -> Path:
    return EPISODES_DIR / episode_id


def audio_dir(episode_id: str) -> Path:
    return episode_dir(episode_id) / "audio"


def clips_dir(episode_id: str) -> Path:
    return episode_dir(episode_id) / "clips"


def manifest_path_for(episode_id: str) -> Path:
    return episode_dir(episode_id) / "manifest.json"


def derive_episode_no(episodes_path: Path = EPISODES_PUBLIC_PATH) -> int:
    """Episode 1 = 1. Steady state = len(public episodes.json) + 1."""
    if not episodes_path.exists():
        return 1
    raw = json.loads(episodes_path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"{episodes_path} is not a JSON list")
    return len(raw) + 1


def derive_hosts(script: EpisodeScript, cast: CastConfig) -> list[str]:
    """Map distinct segment-speaker slugs to display names, anchor first."""
    seen: list[str] = []
    for seg in script.segments:
        if seg.speaker not in seen:
            seen.append(seg.speaker)
    if cast.anchor in seen:
        seen.remove(cast.anchor)
        seen.insert(0, cast.anchor)
    return [cast.cast[s].display_name for s in seen]


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.tmp.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def read_manifest(manifest_path: Path) -> dict[str, Any]:
    return json.loads(manifest_path.read_text())


def write_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    atomic_write_text(manifest_path, json.dumps(manifest, indent=2) + "\n")


_MANIFEST_LOCK = threading.Lock()


def update_segment_state(manifest_path: Path, idx: int, **fields: Any) -> dict[str, Any]:
    with _MANIFEST_LOCK:
        manifest = read_manifest(manifest_path)
        seg = manifest["segments"][idx]
        seg.update(fields)
        write_manifest(manifest_path, manifest)
        return seg


def write_initial_manifest(
    *,
    episode_id: str,
    episode_no: int,
    run_date: str,
    corpus: list[BriefSummary],
    cast: CastConfig,
    script: EpisodeScript,
    overwrite: bool = False,
) -> Path:
    """Write the initial manifest after script generation succeeds.

    Subsequent pipeline phases (TTS, Hedra, stitch, upload) update the
    manifest in-place via atomic rewrites.

    Refuses to clobber an existing manifest unless overwrite=True. Silently
    overwriting would erase per-segment pipeline state (audio_path,
    clip_asset_id, attempts) that downstream phases write after script
    generation.
    """
    mpath = manifest_path_for(episode_id)
    if mpath.exists() and not overwrite:
        raise FileExistsError(
            f"manifest already exists at {mpath}. Pass overwrite=True "
            "to replace (drops all per-segment pipeline state)."
        )
    manifest = {
        "id": episode_id,
        "episode_no": episode_no,
        "run_date": run_date,
        "started_at": datetime.now(tz=timezone.utc).isoformat(),
        "source_brief_ids": [b.id for b in corpus],
        "cast_config_hash": cast_config_hash(),
        "script_model": SCRIPT_MODEL,
        "tts_model": TTS_MODEL,
        "hedra_model": HEDRA_MODEL,
        "hedra_model_id": HEDRA_MODEL_ID,
        "resolution": RESOLUTION,
        "aspect_ratio": ASPECT_RATIO,
        "visibility": DEFAULT_VISIBILITY,
        "validation_status": "script_generated",
        "errors": [],
        "script": script.model_dump(),
        "segments": [
            {
                "idx": i,
                "speaker": seg.speaker,
                "text": seg.text,
                "delivery_note": seg.delivery_note,
                "audio_path": None,
                "audio_status": "pending",
                "clip_path": None,
                "clip_status": "pending",
                "clip_asset_id": None,
                "attempts": 0,
                "errors": [],
            }
            for i, seg in enumerate(script.segments)
        ],
        "stitched_path": None,
        "youtube_id": None,
    }
    write_manifest(mpath, manifest)
    return mpath
