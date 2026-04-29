"""FFmpeg concat + post-stitch ffprobe validation.

Re-encodes with fixed libx264/aac params (deterministic given the same
inputs and the same ffmpeg build). Stream copy would be faster but is
fragile across slight Hedra clip variations.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import (
    EPISODE_DURATION_MAX_SEC,
    EPISODE_DURATION_MIN_SEC,
    REPO_ROOT,
    STITCH_DURATION_TOLERANCE_SEC,
)
from .manifest import episode_dir, read_manifest
from .media import ffprobe_streams


def stitch_episode(*, manifest_path: Path, overwrite: bool = False) -> Path:
    manifest = read_manifest(manifest_path)
    eid = manifest["id"]
    segments = manifest["segments"]
    if any(s.get("clip_status") != "complete" or not s.get("clip_path") for s in segments):
        raise RuntimeError("not all segments are complete — refusing to stitch")

    final_path = episode_dir(eid) / "final.mp4"
    if final_path.exists() and not overwrite:
        raise FileExistsError(f"{final_path} exists. Pass overwrite=True to replace.")

    list_path = episode_dir(eid) / "concat.txt"
    concat_text = "\n".join(
        f"file '{(REPO_ROOT / s['clip_path']).resolve()}'" for s in segments
    ) + "\n"
    list_path.write_text(concat_text)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_path),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "2",
        "-movflags", "+faststart",
        str(final_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg stitch failed:\nSTDERR:\n{proc.stderr}")
    return final_path


def validate_stitched_output(final_path: Path, expected_total_sec: float) -> None:
    """Apply post-stitch validation gates. Raises on failure."""
    if not final_path.exists():
        raise RuntimeError(f"final missing: {final_path}")
    meta = ffprobe_streams(final_path)
    streams = meta.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if len(video) != 1:
        raise RuntimeError(f"expected exactly 1 video stream, got {len(video)}: {final_path}")
    if len(audio) != 1:
        raise RuntimeError(f"expected exactly 1 audio stream, got {len(audio)}: {final_path}")

    v = video[0]
    if int(v["width"]) != 1280 or int(v["height"]) != 720:
        raise RuntimeError(f"final not 1280x720: {v['width']}x{v['height']}")
    if v.get("codec_name") != "h264":
        raise RuntimeError(f"final video codec {v.get('codec_name')!r} != h264")
    if audio[0].get("codec_name") != "aac":
        raise RuntimeError(f"final audio codec {audio[0].get('codec_name')!r} != aac")

    total_sec = float(meta["format"]["duration"])
    if not EPISODE_DURATION_MIN_SEC <= total_sec <= EPISODE_DURATION_MAX_SEC:
        raise RuntimeError(
            f"final duration {total_sec:.2f}s out of bounds "
            f"[{EPISODE_DURATION_MIN_SEC}, {EPISODE_DURATION_MAX_SEC}]"
        )
    delta = abs(total_sec - expected_total_sec)
    if delta > STITCH_DURATION_TOLERANCE_SEC:
        raise RuntimeError(
            f"final duration {total_sec:.2f}s vs expected {expected_total_sec:.2f}s "
            f"differs by {delta:.2f}s > tolerance {STITCH_DURATION_TOLERANCE_SEC}s"
        )
