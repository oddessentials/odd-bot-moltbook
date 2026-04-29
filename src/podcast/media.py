"""ffprobe / ffmpeg helpers + SRT generation.

Pure media-processing helpers; no engine state knowledge beyond reading
the manifest path passed to generate_srt. Behavior testable in isolation.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .manifest import read_manifest, resolve_inside_episode


def ffprobe_streams(path: Path) -> dict[str, Any]:
    """Return ffprobe JSON: format + streams. Raises on probe failure."""
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=30,
    )
    return json.loads(out)


def ffmpeg_mean_volume_db(path: Path) -> float:
    """Compute mean_volume in dB via ffmpeg's volumedetect filter."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i", str(path),
            "-af", "volumedetect",
            "-f", "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    text = proc.stderr or ""
    m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", text)
    if not m:
        raise RuntimeError(f"could not parse mean_volume from ffmpeg stderr:\n{text[-500:]}")
    return float(m.group(1))


def format_srt_timestamp(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms == 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_srt(*, manifest_path: Path) -> Path:
    """Build an SRT from per-segment audio durations.

    Cue timestamps come from cumulative audio durations (ffprobe). The cue
    text is the segment's `text` field. One cue per segment — the script
    is already conversational and segment-sized, no further splitting is
    needed for an audio-only caption track.
    """
    manifest = read_manifest(manifest_path)
    segments = manifest["segments"]
    if any(s.get("audio_status") != "complete" or not s.get("audio_path") for s in segments):
        raise RuntimeError("not all segments have audio — refusing to build SRT")

    cues = []
    cursor = 0.0
    for i, seg in enumerate(segments):
        audio_path = resolve_inside_episode(
            manifest_path=manifest_path,
            recorded_rel=seg.get("audio_path"),
        )
        audio_meta = ffprobe_streams(audio_path)
        dur = float(audio_meta["format"]["duration"])
        start = cursor
        end = cursor + dur
        cues.append(
            f"{i + 1}\n"
            f"{format_srt_timestamp(start)} --> {format_srt_timestamp(end)}\n"
            f"{seg['text']}\n"
        )
        cursor = end

    # SRT is written next to the manifest (operator-supplied filesystem
    # location), not at episode_dir(manifest["id"]) — the manifest field
    # is mutable and cannot direct an output write.
    srt_path = manifest_path.parent / "captions.srt"
    srt_path.write_text("\n".join(cues), encoding="utf-8")
    return srt_path
