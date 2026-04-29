"""Episode manifest read/write/update. The manifest is the canonical state
machine for resume across pipeline phases — everything outside
`data/episodes/<id>/` derives from it.

Atomic writes via tempfile + os.replace (POSIX rename(2) atomicity); a
crash mid-write leaves either the previous contents or no file at all,
never a truncated file. A process-local threading.Lock serialises
in-process concurrent updates from the parallel-segment producer.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .cast import cast_config_hash
from .config import (
    ASPECT_RATIO,
    DEFAULT_VISIBILITY,
    EPISODES_DIR,
    EPISODES_PUBLIC_PATH,
    HEDRA_MODEL,
    HEDRA_MODEL_ID,
    LOCK_PATH,
    REPO_ROOT,
    RESOLUTION,
    SCRIPT_MODEL,
    TTS_MODEL,
)
from .schema import BriefSummary, CastConfig, EpisodeScript


class EpisodeBoundaryError(ValueError):
    """Raised by `resolve_inside_episode` when a manifest-recorded path
    resolves outside the manifest's episode directory.

    Typed so callers (cmd_upload, generate_srt, stitch_episode, etc.)
    can distinguish a sandbox violation from other ValueErrors and
    refuse the operation rather than crash with a generic error.
    """


def resolve_inside_dir(
    *,
    boundary: Path,
    recorded_rel: str | None,
    repo_root: Path | None = None,
) -> Path:
    """Resolve a recorded relative path against `repo_root` and confirm
    it lies inside `boundary`.

    Generic version used by both the episode-dir sandbox and the
    docs/podcast/<id>/ OG-output sandbox. `repo_root` defaults to the
    module-level REPO_ROOT, looked up at call time so tests can patch
    `manifest.REPO_ROOT` without re-importing.

    Returns the canonical resolved Path on success. Raises
    EpisodeBoundaryError on:
      - empty / None recorded_rel
      - newline / carriage return in recorded_rel or resolved path
      - resolution outside `boundary` (after `..` collapsing and
        symlink following)
    """
    if not recorded_rel:
        raise EpisodeBoundaryError(f"empty recorded path under {boundary}")
    if "\n" in recorded_rel or "\r" in recorded_rel:
        raise EpisodeBoundaryError(
            f"recorded path contains a newline: {recorded_rel!r}"
        )
    effective_root = repo_root if repo_root is not None else REPO_ROOT
    candidate = (effective_root / recorded_rel).resolve(strict=False)
    candidate_str = str(candidate)
    if "\n" in candidate_str or "\r" in candidate_str:
        raise EpisodeBoundaryError(
            f"resolved path contains a newline: {candidate!r}"
        )
    boundary_resolved = boundary.resolve(strict=False)
    if not candidate.is_relative_to(boundary_resolved):
        raise EpisodeBoundaryError(
            f"recorded path {recorded_rel!r} resolves to {candidate} "
            f"which escapes boundary {boundary_resolved}"
        )
    return candidate


def resolve_inside_episode(
    *,
    manifest_path: Path,
    recorded_rel: str | None,
    repo_root: Path | None = None,
) -> Path:
    """Resolve a manifest-recorded path inside the manifest's episode
    directory (`manifest_path.parent`).

    Thin wrapper over `resolve_inside_dir` that anchors on the operator-
    supplied filesystem location. manifest["id"] is mutable and cannot
    be the source of the sandbox limit.
    """
    return resolve_inside_dir(
        boundary=manifest_path.parent,
        recorded_rel=recorded_rel,
        repo_root=repo_root,
    )


@contextlib.contextmanager
def acquire_run_lock(path: Path = LOCK_PATH) -> Iterator[None]:
    """Process-exclusive non-blocking flock at the podcast lock path.

    Mirrors src/publish.py's acquire_lock pattern. Yields on acquisition;
    raises BlockingIOError if the lock is held by another process. Caller
    is expected to catch BlockingIOError at the CLI boundary and exit 0
    cleanly (sibling run in progress).

    Lock is auto-released by the kernel on process death, so no stale-lock
    recovery is needed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


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


# Ordered phase markers. Only advances are written by `advance_validation_status`
# so that re-running an earlier phase (e.g., produce-segments after the episode
# already uploaded) doesn't roll back a later phase's completion marker.
VALIDATION_STATUS_ORDER: tuple[str, ...] = (
    "script_generated",
    "segments_complete",
    "stitched",
    "video_uploaded",
    "uploaded",
    "published",
)


def advance_validation_status(manifest_path: Path, target: str) -> str:
    """Set `validation_status` to `target` only if `target` is at or past the
    current state in `VALIDATION_STATUS_ORDER`. Returns the resulting status
    so callers can log what actually landed.

    Raises ValueError if `target` is not a known phase marker.
    """
    if target not in VALIDATION_STATUS_ORDER:
        raise ValueError(f"unknown validation_status: {target!r}")
    target_idx = VALIDATION_STATUS_ORDER.index(target)
    with _MANIFEST_LOCK:
        manifest = read_manifest(manifest_path)
        current = manifest.get("validation_status")
        try:
            current_idx = VALIDATION_STATUS_ORDER.index(current) if current else -1
        except ValueError:
            current_idx = -1
        if target_idx > current_idx:
            manifest["validation_status"] = target
            write_manifest(manifest_path, manifest)
            return target
        return current  # type: ignore[return-value]


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
