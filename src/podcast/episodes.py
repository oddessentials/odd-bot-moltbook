"""data/episodes.json publish-event writer.

This is Phase 2's bridge from the internal manifest state machine to the
public publish surface. The SPA reads `data/episodes.json` (Phase 2.3
flips the data source from `agent-brief/.../content.ts:episodes[]`) and
the steady-state X-post workflow (Phase 3) watches the same file.

The write only happens after every hard gate passes. Partial success is
not a publish event — a missing caption, a failed re-verification on
YouTube, an unresolvable artifact path, an OG page that didn't generate
all flunk the gate and leave `data/episodes.json` untouched. The
operator sees the gate that failed; no public surface advances on
incomplete state.

Hard gates (in order, all required):

  G1: youtube_id present in manifest AND videos.list confirms the video
      is owned by the authenticated channel.
  G2: episode_record present in manifest AND validates as EpisodeRecord.
  G3: stitched_path resolves inside the episode dir, file exists, and
      ffprobe says duration is in [EPISODE_DURATION_MIN_SEC,
      EPISODE_DURATION_MAX_SEC].
  G4: youtube_caption_id present in manifest (caption track was
      uploaded for the videoId in G1).
  G5: og_html_path present in manifest AND resolves inside
      docs/podcast/<id>/ AND the file exists. (The path field is
      populated by Phase 2.2's OG generator. Until that lands, G5
      always fails — fail-closed.)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import RootModel, ValidationError

from .config import (
    EPISODE_DURATION_MAX_SEC,
    EPISODE_DURATION_MIN_SEC,
    EPISODES_PUBLIC_PATH,
    PODCAST_OG_DIR,
)
from .manifest import (
    EpisodeBoundaryError,
    atomic_write_text,
    read_manifest,
    resolve_inside_dir,
    resolve_inside_episode,
)
from .media import ffprobe_streams
from .schema import EpisodeRecord


class PublishGateError(RuntimeError):
    """Raised when a publish-event hard gate fails. The error message
    names the gate (G1..G5) so the operator can find what to fix."""


class _EpisodesPayload(RootModel[list[EpisodeRecord]]):
    """data/episodes.json shape: a list of validated Episode records."""


def _read_episodes_json(path: Path = EPISODES_PUBLIC_PATH) -> list[EpisodeRecord]:
    """Read + validate the existing episodes.json. Returns an empty list
    if the file doesn't exist (first publish bootstraps it)."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise PublishGateError(f"data/episodes.json is not valid JSON: {e}")
    try:
        validated = _EpisodesPayload.model_validate(raw)
    except ValidationError as e:
        raise PublishGateError(f"data/episodes.json fails Episode shape validation: {e}")
    return list(validated.root)


def _write_episodes_json(
    episodes: list[EpisodeRecord],
    path: Path = EPISODES_PUBLIC_PATH,
) -> None:
    """Sort ascending by id, dump JSON, atomic write."""
    sorted_eps = sorted(episodes, key=lambda e: e.id)
    payload = [e.model_dump() for e in sorted_eps]
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def _gate_g1_youtube_verify(*, manifest: dict, credentials) -> str:
    """G1: youtube_id present + videos.list confirms ownership.

    Returns the verified videoId. Raises PublishGateError on failure.
    """
    video_id = manifest.get("youtube_id")
    if not video_id:
        raise PublishGateError("G1 youtube_id missing from manifest")
    if credentials is None:
        raise PublishGateError("G1 credentials required to verify videoId")
    from .youtube import verify_youtube_video
    try:
        record = verify_youtube_video(credentials=credentials, video_id=video_id)
    except Exception as e:
        raise PublishGateError(f"G1 videos.list failed for {video_id!r}: {e}") from e
    if record.get("id") != video_id:
        raise PublishGateError(
            f"G1 videos.list returned id {record.get('id')!r} != {video_id!r}"
        )
    return video_id


def _gate_g2_episode_record(*, manifest: dict) -> EpisodeRecord:
    """G2: episode_record present + validates as EpisodeRecord."""
    raw = manifest.get("episode_record")
    if not raw:
        raise PublishGateError("G2 episode_record missing from manifest")
    try:
        return EpisodeRecord.model_validate(raw)
    except ValidationError as e:
        raise PublishGateError(f"G2 episode_record fails Episode shape: {e}") from e


def _gate_g3_final_mp4(*, manifest: dict, manifest_path: Path) -> Path:
    """G3: stitched_path resolves + exists + ffprobe duration in bounds.

    Returns the resolved final.mp4 Path. Raises PublishGateError on
    failure.
    """
    rel = manifest.get("stitched_path")
    try:
        final_path = resolve_inside_episode(
            manifest_path=manifest_path, recorded_rel=rel,
        )
    except EpisodeBoundaryError as e:
        raise PublishGateError(f"G3 stitched_path sandbox refused: {e}") from e
    if not final_path.exists():
        raise PublishGateError(f"G3 final.mp4 missing on disk: {final_path}")
    try:
        meta = ffprobe_streams(final_path)
    except Exception as e:
        raise PublishGateError(f"G3 ffprobe failed for {final_path}: {e}") from e
    try:
        duration = float(meta["format"]["duration"])
    except (KeyError, TypeError, ValueError) as e:
        raise PublishGateError(f"G3 final.mp4 has no parseable duration: {e}") from e
    if not EPISODE_DURATION_MIN_SEC <= duration <= EPISODE_DURATION_MAX_SEC:
        raise PublishGateError(
            f"G3 final.mp4 duration {duration:.2f}s out of bounds "
            f"[{EPISODE_DURATION_MIN_SEC}, {EPISODE_DURATION_MAX_SEC}]"
        )
    return final_path


def _gate_g4_caption(*, manifest: dict) -> str:
    """G4: youtube_caption_id present (caption track was uploaded)."""
    caption_id = manifest.get("youtube_caption_id")
    if not caption_id:
        raise PublishGateError("G4 youtube_caption_id missing from manifest")
    return caption_id


def _gate_g5_og_html(*, manifest: dict, episode_id: str) -> Path:
    """G5: og_html_path present + resolves inside docs/podcast/<id>/.

    The path field is populated by Phase 2.2's OG generator. Until that
    lands, this gate always fails (fail-closed) — which is the intended
    behavior for sub-piece 1: the publish writer is wired and tested,
    but no real episode publishes until OG generation is in.
    """
    rel = manifest.get("og_html_path")
    if not rel:
        raise PublishGateError(
            "G5 og_html_path missing from manifest "
            "(generated by the Phase 2.2 OG-page step)"
        )
    boundary = PODCAST_OG_DIR / episode_id
    try:
        og_path = resolve_inside_dir(boundary=boundary, recorded_rel=rel)
    except EpisodeBoundaryError as e:
        raise PublishGateError(f"G5 og_html_path sandbox refused: {e}") from e
    if not og_path.exists():
        raise PublishGateError(f"G5 OG page missing on disk: {og_path}")
    return og_path


def publish_episode(
    *,
    manifest_path: Path,
    credentials,
    episodes_path: Path = EPISODES_PUBLIC_PATH,
) -> EpisodeRecord:
    """Run every hard gate; on full success, append/update
    `data/episodes.json` and return the record that was written.

    Raises PublishGateError if any gate fails. data/episodes.json is
    NEVER written on partial success — the file is the public publish
    surface and a malformed entry there has user-visible blast radius.
    """
    manifest = read_manifest(manifest_path)
    episode_id = manifest_path.parent.name  # filesystem-derived, not manifest["id"]

    # Gates run in declared order so failures point at the earliest
    # missing piece. Each gate that needs the same value (videoId,
    # final_path) recomputes from the manifest+filesystem to avoid
    # passing trust state between gates.
    _gate_g1_youtube_verify(manifest=manifest, credentials=credentials)
    record = _gate_g2_episode_record(manifest=manifest)
    _gate_g3_final_mp4(manifest=manifest, manifest_path=manifest_path)
    _gate_g4_caption(manifest=manifest)
    _gate_g5_og_html(manifest=manifest, episode_id=episode_id)

    # All gates passed. Read existing, dedup by id (record's id is the
    # canonical identity), append/update, sort ascending, atomic write.
    existing = _read_episodes_json(episodes_path)
    by_id: dict[str, EpisodeRecord] = {e.id: e for e in existing}
    by_id[record.id] = record
    _write_episodes_json(list(by_id.values()), episodes_path)
    return record


def cmd_publish(args: Any) -> int:
    """CLI entrypoint. Operator runs this AFTER cmd_upload has succeeded
    (and, eventually, after the OG generator has populated og_html_path
    in the manifest)."""
    from .keys import load_youtube_credentials
    from .manifest import (
        advance_validation_status,
        manifest_path_for,
    )

    eid = args.episode_id
    mpath = manifest_path_for(eid)
    if not mpath.exists():
        print(f"manifest missing at {mpath}", file=sys.stderr)
        return 2
    creds = load_youtube_credentials()
    try:
        record = publish_episode(manifest_path=mpath, credentials=creds)
    except PublishGateError as e:
        print(f"publish refused: {e}", file=sys.stderr)
        return 2
    advance_validation_status(mpath, "published")
    print(f"published {record.id} (episodeNo={record.episodeNo}) → data/episodes.json")
    return 0
