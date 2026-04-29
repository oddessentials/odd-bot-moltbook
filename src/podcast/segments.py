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
    audio_dir,
    clips_dir,
    read_manifest,
    update_segment_state,
)
from .media import ffmpeg_mean_volume_db, ffprobe_streams
from .schema import CastConfig
from .tts import generate_tts


def validate_segment_outputs(audio_path: Path, clip_path: Path) -> None:
    """Apply the canary gates. Raises RuntimeError on any failure.

    Gates (objective):
      - TTS file exists and is ≥ TTS_MIN_BYTES.
      - TTS mean_volume above silence threshold.
      - TTS duration in [TTS_MIN_DURATION_SEC, TTS_MAX_DURATION_SEC].
      - Clip has both video and audio streams.
      - Clip resolution ≈ 720p with 16:9 aspect.
      - Clip duration matches TTS duration ±CLIP_AUDIO_DURATION_TOLERANCE_SEC.
    """
    if not audio_path.exists():
        raise RuntimeError(f"audio missing: {audio_path}")
    audio_bytes = audio_path.stat().st_size
    if audio_bytes < TTS_MIN_BYTES:
        raise RuntimeError(f"audio too small ({audio_bytes} bytes): {audio_path}")

    audio_meta = ffprobe_streams(audio_path)
    audio_duration_sec = float(audio_meta["format"]["duration"])
    if not TTS_MIN_DURATION_SEC <= audio_duration_sec <= TTS_MAX_DURATION_SEC:
        raise RuntimeError(
            f"audio duration {audio_duration_sec:.2f}s out of bounds "
            f"[{TTS_MIN_DURATION_SEC}, {TTS_MAX_DURATION_SEC}]"
        )

    mean_db = ffmpeg_mean_volume_db(audio_path)
    if mean_db < TTS_MIN_MEAN_VOLUME_DB:
        raise RuntimeError(
            f"audio mean_volume {mean_db:.1f} dB below silence threshold "
            f"{TTS_MIN_MEAN_VOLUME_DB} dB — TTS likely produced silence"
        )

    if not clip_path.exists():
        raise RuntimeError(f"clip missing: {clip_path}")
    clip_meta = ffprobe_streams(clip_path)
    streams = clip_meta.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not video_streams:
        raise RuntimeError(f"clip has no video stream: {clip_path}")
    if not audio_streams:
        raise RuntimeError(f"clip has no audio stream: {clip_path}")

    v = video_streams[0]
    width = int(v["width"])
    height = int(v["height"])
    if height != EXPECTED_VIDEO_HEIGHT:
        raise RuntimeError(
            f"clip height {height} != expected {EXPECTED_VIDEO_HEIGHT}: {clip_path}"
        )
    aspect = width / height
    if abs(aspect - EXPECTED_ASPECT_RATIO) / EXPECTED_ASPECT_RATIO > EXPECTED_ASPECT_RATIO_TOLERANCE:
        raise RuntimeError(
            f"clip aspect {aspect:.4f} not 16:9 (within {EXPECTED_ASPECT_RATIO_TOLERANCE * 100:.0f}%): "
            f"{width}x{height}"
        )

    clip_duration_sec = float(clip_meta["format"]["duration"])
    delta = abs(clip_duration_sec - audio_duration_sec)
    if delta > CLIP_AUDIO_DURATION_TOLERANCE_SEC:
        raise RuntimeError(
            f"clip duration {clip_duration_sec:.2f}s vs audio {audio_duration_sec:.2f}s "
            f"differs by {delta:.2f}s > tolerance {CLIP_AUDIO_DURATION_TOLERANCE_SEC}s"
        )


def process_segment(
    *,
    manifest_path: Path,
    idx: int,
    cast: CastConfig,
    elevenlabs_key: str,
    hedra_session: requests.Session,
) -> None:
    """Run TTS + Hedra clip for one segment and update the manifest.

    Idempotent-ish: if both audio and clip files exist on disk and the
    manifest already records them as complete, skip and return. (Simple
    resume — full state-machine rigor lands in Phase 1.)
    """
    manifest = read_manifest(manifest_path)
    seg = manifest["segments"][idx]
    speaker = seg["speaker"]
    text = seg["text"]
    member = cast.cast.get(speaker)
    if member is None:
        raise RuntimeError(f"segment {idx} speaker {speaker!r} not in cast {cast.slugs()}")

    eid = manifest["id"]
    audio_path = audio_dir(eid) / f"seg{idx:02d}.mp3"
    clip_path = clips_dir(eid) / f"seg{idx:02d}.mp4"

    if (
        seg.get("audio_status") == "complete"
        and seg.get("clip_status") == "complete"
        and audio_path.exists()
        and clip_path.exists()
    ):
        print(f"  seg{idx:02d}: already complete, skipping")
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
