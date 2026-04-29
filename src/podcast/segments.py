"""Per-segment processing + canary validation.

`process_segment` is the unit of work in the canary-then-scale pipeline:
TTS → Hedra audio upload → Hedra clip submission → poll → download →
manifest update → objective validation. `validate_segment_outputs` is
the gate function applied to every segment regardless of canary order.
"""

from __future__ import annotations

import time
from pathlib import Path

import requests

from .config import (
    CLIP_AUDIO_DURATION_TOLERANCE_SEC,
    EXPECTED_ASPECT_RATIO,
    EXPECTED_ASPECT_RATIO_TOLERANCE,
    EXPECTED_VIDEO_HEIGHT,
    REPO_ROOT,
    SEGMENT_MAX_ATTEMPTS,
    TTS_MAX_DURATION_SEC,
    TTS_MIN_BYTES,
    TTS_MIN_DURATION_SEC,
    TTS_MIN_MEAN_VOLUME_DB,
)
from .hedra import (
    download_clip,
    poll_hedra_clip,
    submit_hedra_clip,
    upload_hedra_audio,
)
from .manifest import (
    EpisodeBoundaryError,
    read_manifest,
    resolve_inside_episode,
    update_segment_state,
)
from .media import ffmpeg_mean_volume_db, ffprobe_streams
from .schema import CastConfig
from .tts import generate_tts


class SegmentValidationError(RuntimeError):
    """Raised by validate_segment_outputs when an objective gate fails.

    Typed so the retry wrapper and the process_segment idempotency-skip
    path can distinguish a validation failure (segment artifacts on disk
    are bad and need to be re-rendered) from a transient error
    (network/HTTP/queue glitch where the existing artifacts are still
    fine to reuse).
    """


def validate_segment_outputs(audio_path: Path, clip_path: Path) -> None:
    """Apply the canary gates. Raises SegmentValidationError on any failure.

    Gates (objective):
      - TTS file exists and is ≥ TTS_MIN_BYTES.
      - TTS mean_volume above silence threshold.
      - TTS duration in [TTS_MIN_DURATION_SEC, TTS_MAX_DURATION_SEC].
      - Clip has both video and audio streams.
      - Clip resolution ≈ 720p with 16:9 aspect.
      - Clip duration matches TTS duration ±CLIP_AUDIO_DURATION_TOLERANCE_SEC.
    """
    if not audio_path.exists():
        raise SegmentValidationError(f"audio missing: {audio_path}")
    audio_bytes = audio_path.stat().st_size
    if audio_bytes < TTS_MIN_BYTES:
        raise SegmentValidationError(f"audio too small ({audio_bytes} bytes): {audio_path}")

    audio_meta = ffprobe_streams(audio_path)
    audio_duration_sec = float(audio_meta["format"]["duration"])
    if not TTS_MIN_DURATION_SEC <= audio_duration_sec <= TTS_MAX_DURATION_SEC:
        raise SegmentValidationError(
            f"audio duration {audio_duration_sec:.2f}s out of bounds "
            f"[{TTS_MIN_DURATION_SEC}, {TTS_MAX_DURATION_SEC}]"
        )

    mean_db = ffmpeg_mean_volume_db(audio_path)
    if mean_db < TTS_MIN_MEAN_VOLUME_DB:
        raise SegmentValidationError(
            f"audio mean_volume {mean_db:.1f} dB below silence threshold "
            f"{TTS_MIN_MEAN_VOLUME_DB} dB — TTS likely produced silence"
        )

    if not clip_path.exists():
        raise SegmentValidationError(f"clip missing: {clip_path}")
    clip_meta = ffprobe_streams(clip_path)
    streams = clip_meta.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not video_streams:
        raise SegmentValidationError(f"clip has no video stream: {clip_path}")
    if not audio_streams:
        raise SegmentValidationError(f"clip has no audio stream: {clip_path}")

    v = video_streams[0]
    width = int(v["width"])
    height = int(v["height"])
    if height != EXPECTED_VIDEO_HEIGHT:
        raise SegmentValidationError(
            f"clip height {height} != expected {EXPECTED_VIDEO_HEIGHT}: {clip_path}"
        )
    aspect = width / height
    if abs(aspect - EXPECTED_ASPECT_RATIO) / EXPECTED_ASPECT_RATIO > EXPECTED_ASPECT_RATIO_TOLERANCE:
        raise SegmentValidationError(
            f"clip aspect {aspect:.4f} not 16:9 (within {EXPECTED_ASPECT_RATIO_TOLERANCE * 100:.0f}%): "
            f"{width}x{height}"
        )

    clip_duration_sec = float(clip_meta["format"]["duration"])
    delta = abs(clip_duration_sec - audio_duration_sec)
    if delta > CLIP_AUDIO_DURATION_TOLERANCE_SEC:
        raise SegmentValidationError(
            f"clip duration {clip_duration_sec:.2f}s vs audio {audio_duration_sec:.2f}s "
            f"differs by {delta:.2f}s > tolerance {CLIP_AUDIO_DURATION_TOLERANCE_SEC}s"
        )


def is_segment_complete_and_valid(
    *,
    manifest_path: Path,
    seg: dict,
    idx: int,
) -> bool:
    """Decide whether `seg`'s already-recorded artifacts let us skip work.

    Treats the manifest as source of truth for the artifact PATHS but
    NOT for the boundary they must resolve inside. The boundary is
    derived from `manifest_path.parent` (operator-supplied filesystem
    location) via the shared `resolve_inside_episode` helper — so a
    tampered `manifest["id"]` cannot widen or relocate the sandbox.
    Recorded artifacts must resolve inside that path; `..` segments,
    absolute paths, and escaping symlinks are all refused.

    If the manifest-recorded artifacts are present, sandboxed, and pass
    `validate_segment_outputs`, returns True and the caller should
    short-circuit. If they fail any of those checks, the segment's
    audio/clip status flags are reset to "pending" and False is
    returned so the caller falls through to re-render at convention
    paths.
    """
    if seg.get("audio_status") != "complete" or seg.get("clip_status") != "complete":
        return False
    audio_rel = seg.get("audio_path")
    clip_rel = seg.get("clip_path")
    if not audio_rel or not clip_rel:
        return False

    try:
        audio_path = resolve_inside_episode(manifest_path=manifest_path, recorded_rel=audio_rel)
        clip_path = resolve_inside_episode(manifest_path=manifest_path, recorded_rel=clip_rel)
    except EpisodeBoundaryError as e:
        print(
            f"  seg{idx:02d}: {e}; refusing to validate, resetting status "
            "and re-rendering at convention paths."
        )
        update_segment_state(
            manifest_path,
            idx,
            audio_status="pending",
            clip_status="pending",
        )
        return False

    if not audio_path.exists() or not clip_path.exists():
        return False
    try:
        validate_segment_outputs(audio_path, clip_path)
    except SegmentValidationError as e:
        print(
            f"  seg{idx:02d}: previously-complete segment failed re-validation "
            f"({e}); resetting status and re-rendering."
        )
        update_segment_state(
            manifest_path,
            idx,
            audio_status="pending",
            clip_status="pending",
        )
        return False
    print(f"  seg{idx:02d}: already complete, skipping")
    return True


def process_segment(
    *,
    manifest_path: Path,
    idx: int,
    cast: CastConfig,
    elevenlabs_key: str,
    hedra_session: requests.Session,
) -> None:
    """Run TTS + Hedra clip for one segment and update the manifest.

    Idempotent-ish: if the manifest records both artifacts as complete,
    points at on-disk files, and those files pass `validate_segment_
    outputs`, skip and return. Otherwise re-render at convention paths.
    """
    manifest = read_manifest(manifest_path)
    seg = manifest["segments"][idx]
    speaker = seg["speaker"]
    text = seg["text"]
    member = cast.cast.get(speaker)
    if member is None:
        raise RuntimeError(f"segment {idx} speaker {speaker!r} not in cast {cast.slugs()}")

    # Convention paths are derived structurally from the manifest's own
    # filesystem location. Anchoring on manifest_path.parent (rather than
    # manifest["id"] via audio_dir/clips_dir) keeps a tampered manifest
    # id field from directing fresh-render writes outside the episode
    # directory.
    work_dir = manifest_path.parent
    audio_path = work_dir / "audio" / f"seg{idx:02d}.mp3"
    clip_path = work_dir / "clips" / f"seg{idx:02d}.mp4"

    if is_segment_complete_and_valid(manifest_path=manifest_path, seg=seg, idx=idx):
        return

    seg["attempts"] = int(seg.get("attempts", 0)) + 1
    update_segment_state(manifest_path, idx, attempts=seg["attempts"])

    print(f"  seg{idx:02d} [{speaker}]: TTS ({len(text.split())} words)...")
    generate_tts(
        text=text,
        voice_id=member.elevenlabs_voice_id,
        out_path=audio_path,
        api_key=elevenlabs_key,
    )
    update_segment_state(
        manifest_path, idx,
        audio_path=str(audio_path.relative_to(REPO_ROOT)),
        audio_status="complete",
    )

    print(f"  seg{idx:02d}: uploading audio to Hedra...")
    audio_asset_id = upload_hedra_audio(hedra_session, audio_path)

    print(f"  seg{idx:02d}: submitting Hedra Character-3 generation...")
    text_prompt = (
        f"{member.persona}. Speaking calmly and clearly to camera, "
        f"natural lip sync, {member.display_name}'s usual cadence."
    )
    gen_id = submit_hedra_clip(
        hedra_session,
        image_asset_id=member.hedra_image_asset_id,
        audio_asset_id=audio_asset_id,
        text_prompt=text_prompt,
    )
    t0 = time.time()
    clip_asset_id, download_url = poll_hedra_clip(hedra_session, gen_id)
    elapsed = time.time() - t0
    print(f"  seg{idx:02d}: clip rendered in {elapsed:.1f}s, downloading...")
    download_clip(download_url, clip_path)
    update_segment_state(
        manifest_path, idx,
        clip_path=str(clip_path.relative_to(REPO_ROOT)),
        clip_status="complete",
        clip_asset_id=clip_asset_id,
    )

    print(f"  seg{idx:02d}: validating canary gates...")
    validate_segment_outputs(audio_path, clip_path)
    print(f"  seg{idx:02d}: ok")


def process_segment_with_retry(
    *,
    manifest_path: Path,
    idx: int,
    cast: CastConfig,
    elevenlabs_key: str,
    hedra_session: requests.Session,
    max_attempts: int = SEGMENT_MAX_ATTEMPTS,
) -> None:
    """Bounded retry around `process_segment`.

    Each attempt's failure is appended to `manifest.segments[idx].errors`
    so post-mortem can read the full failure history without combing logs.
    Idempotency in process_segment lets completed sub-steps (audio
    written, clip rendered) survive across attempts — we don't pay the
    full cost on every retry, only the steps that didn't make it.

    Raises RuntimeError when the budget is exhausted; the original
    failure is chained as `__cause__`.
    """
    last_err: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            process_segment(
                manifest_path=manifest_path,
                idx=idx,
                cast=cast,
                elevenlabs_key=elevenlabs_key,
                hedra_session=hedra_session,
            )
            return
        except Exception as e:
            last_err = e
            seg = read_manifest(manifest_path)["segments"][idx]
            existing_errors = list(seg.get("errors") or [])
            existing_errors.append(
                {
                    "attempt": attempt,
                    "type": type(e).__name__,
                    "message": str(e)[:500],
                }
            )
            update_segment_state(manifest_path, idx, errors=existing_errors)
            if attempt >= max_attempts:
                break
            print(
                f"  seg{idx:02d}: attempt {attempt}/{max_attempts} failed: "
                f"{type(e).__name__}: {e} — retrying"
            )
    raise RuntimeError(
        f"segment {idx} failed after {max_attempts} attempts: {last_err}"
    ) from last_err
