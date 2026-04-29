"""Podcast orchestrator scaffold (Phase 0b — Episode 1 proof).

Generates a fully automated weekly AI-news video podcast from the existing
published daily briefs. Mirrors the orchestrator shape of `src/publish.py`
(deferred state writes, segment-level retry, validation gates) but is wholly
separate from the daily brief publish path. The locked plan lives at
`plans/podcast-pipeline.md`.

Phase 0b scope (this scaffold):

  1. Acquire process-exclusive lock at a path distinct from the daily
     publish lock so a daily run and a podcast run can interleave safely
     (plan §Phase 4 "Lock path distinct from the daily lock").

  2. Load eligible corpus from `data/briefs.json` — filter to
     `status == "published"` AND `id` matches the daily date shape
     (`^\\d{4}-\\d{2}-\\d{2}$`). The grandfathered weekly artifact
     `2026-W18` is excluded by this shape filter (plan §Locked decisions —
     Episode 1 corpus). Episode 1 uses every currently eligible daily.

  3. [DEFERRED — design check-in] Script generation:
        - Prompt LLM with corpus + cast personas to produce structured
          two-host segments.
        - Validate against a Pydantic segment schema.
        - Bounded retries.

  4. [DEFERRED] Per-segment TTS via ElevenLabs (cast voice IDs from
     `config/podcast-cast.yaml`).

  5. [DEFERRED] Per-segment Hedra Character-3 clip generation
     (cast image asset IDs from `config/podcast-cast.yaml`).

  6. [DEFERRED] FFmpeg deterministic stitch into a single MP4.

  7. [DEFERRED] SRT caption generation from segment timing + YouTube
     caption-track upload (decision locked in PR #3 review).

  8. [DEFERRED] Unlisted YouTube upload via the OAuth refresh token in
     `.keys`; verify `videoId` via `videos.list`.

  9. [DEFERRED] Episode metadata write matching the SPA `Episode` shape
     at `agent-brief/client/src/data/content.ts:61-70`.

Invariants this module must enforce (asserted once the deferred steps
land):

  - Identity-mapping stability: `voice_id` + `hedra_image_asset_id` are
    inputs read from cast config, never created/mutated at runtime.
  - Segment-level retry only — never episode-level.
  - YouTube upload is unlisted in Phase 0; the public flip is a separate
    publish-event concern handled in Phase 2.
  - No writes to `data/briefs.json`, `data/x-posts.jsonl`,
    `scripts/run-daily-publish.sh`, `src/publish.py`,
    `.github/workflows/x-post.yml`, or any daily-pipeline artifact.

Phase 0b exit criteria (from plan):

  - Episode 1 generated automatically from current eligible content.
  - Cast identity is stable and contract-driven.
  - Final MP4 passes FFprobe validation.
  - YouTube upload returns a verifiable `videoId`.
  - No live-system behavior changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import date as _date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Literal

import anthropic
import requests
import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
BRIEFS_PATH = DATA_DIR / "briefs.json"
EPISODES_DIR = DATA_DIR / "episodes"
EPISODES_PUBLIC_PATH = DATA_DIR / "episodes.json"
LOCK_PATH = DATA_DIR / ".podcast.run.lock"
CAST_CONFIG_PATH = REPO_ROOT / "config" / "podcast-cast.yaml"

DAILY_ID = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SCRIPT_MODEL = "claude-sonnet-4-6"
HEDRA_MODEL = "hedra_character_3"
HEDRA_MODEL_ID = "d1dd37a3-e39a-4854-a298-6510289f9cf2"
TTS_MODEL = "eleven_multilingual_v2"
RESOLUTION = "720p"
ASPECT_RATIO = "16:9"
DEFAULT_VISIBILITY = "unlisted"

ANTHROPIC_KEY_PATH = Path.home() / ".openclaw" / "keys" / "moltbook-engine-anthropic-api-key"

PODCAST_KEYS_FILE = REPO_ROOT / ".keys"
HEDRA_API_BASE = "https://api.hedra.com/web-app/public"
ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"

# Canary gates (objective, applied to every segment).
EXPECTED_VIDEO_HEIGHT = 720           # 720p
EXPECTED_ASPECT_RATIO_TOLERANCE = 0.02  # tolerate ±2% on 16:9 = 1.778
EXPECTED_ASPECT_RATIO = 16 / 9
TTS_MIN_BYTES = 1024
TTS_MIN_DURATION_SEC = 2.0
TTS_MAX_DURATION_SEC = 30.0
TTS_MIN_MEAN_VOLUME_DB = -55.0   # mean_volume above this counts as non-silent
CLIP_AUDIO_DURATION_TOLERANCE_SEC = 1.0


class CastMember(BaseModel):
    display_name: str
    role: str
    persona: str
    elevenlabs_voice_id: str
    hedra_image_asset_id: str
    local_reference_path: str | None = None


class CastConfig(BaseModel):
    version: int
    anchor: str
    cast: dict[str, CastMember]

    @field_validator("cast")
    @classmethod
    def cast_nonempty(cls, v: dict[str, CastMember]) -> dict[str, CastMember]:
        if not v:
            raise ValueError("cast must contain at least one member")
        return v

    def slugs(self) -> list[str]:
        return list(self.cast.keys())


class Segment(BaseModel):
    speaker: str
    text: str = Field(..., min_length=1)
    delivery_note: str | None = Field(None, max_length=200)

    @field_validator("text")
    @classmethod
    def word_count(cls, v: str) -> str:
        wc = len(v.split())
        if not 12 <= wc <= 45:
            raise ValueError(f"segment text word count {wc} not in [12, 45]")
        return v


class EpisodeScript(BaseModel):
    title: str = Field(..., min_length=1, max_length=80)
    description: str = Field(..., min_length=100, max_length=500)
    segments: list[Segment] = Field(..., min_length=8, max_length=16)


class EpisodeRecord(BaseModel):
    """Engine output mirroring the SPA Episode TS interface at
    agent-brief/client/src/data/content.ts:61-70. Phase 0b validates the
    shape and stores it in the gitignored manifest only — no public
    episodes.json write until Phase 2."""

    id: str
    episodeNo: int = Field(..., ge=1)
    title: str = Field(..., min_length=1, max_length=80)
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    durationMinutes: int = Field(..., ge=1)
    youtubeId: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    hosts: list[str] = Field(..., min_length=1)


@dataclass(frozen=True)
class BriefSummary:
    """A single published daily brief, narrowed to the fields the script
    generator needs. Full record stays in `data/briefs.json` — this is a
    derived view, not a parallel source of truth."""

    id: str
    issue_no: int
    date: str
    title: str
    dek: str
    items: tuple[dict, ...]


def load_eligible_corpus(briefs_path: Path = BRIEFS_PATH) -> list[BriefSummary]:
    """Return the Episode 1 corpus — every published daily-shape brief.

    Filter mirrors plan §Locked decisions exactly:
      - status == "published"
      - id matches `^\\d{4}-\\d{2}-\\d{2}$`

    The weekly artifact `2026-W18` is excluded by the id-shape filter.
    Order: ascending by id so the script generator sees chronological
    flow.
    """
    raw = json.loads(briefs_path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"{briefs_path} is not a JSON list (got {type(raw).__name__})")

    out: list[BriefSummary] = []
    for r in raw:
        if r.get("status") != "published":
            continue
        bid = r.get("id", "")
        if not DAILY_ID.match(bid):
            continue
        out.append(
            BriefSummary(
                id=bid,
                issue_no=int(r["issueNo"]),
                date=r["date"],
                title=r["title"],
                dek=r["dek"],
                items=tuple(r.get("items", [])),
            )
        )
    out.sort(key=lambda b: b.id)
    return out


def _summarize_corpus(corpus: Iterable[BriefSummary]) -> str:
    lines = []
    for b in corpus:
        lines.append(f"  - {b.id} (issue {b.issue_no}): {b.title!r} — {len(b.items)} items")
    return "\n".join(lines)


def load_cast(path: Path = CAST_CONFIG_PATH) -> CastConfig:
    cfg = CastConfig.model_validate(yaml.safe_load(path.read_text()))
    if cfg.anchor not in cfg.cast:
        raise ValueError(f"anchor {cfg.anchor!r} not in cast slugs {list(cfg.cast.keys())}")
    return cfg


def cast_config_hash(path: Path = CAST_CONFIG_PATH) -> str:
    """Stable 12-char fingerprint of the cast config bytes.

    Used in the episode manifest to record which cast contract produced the
    episode. Hashing the file bytes (not the parsed Pydantic dump) keeps the
    fingerprint comparable from outside the engine.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


# ---- Script generation -----------------------------------------------------

@lru_cache(maxsize=1)
def _anthropic_client() -> anthropic.Anthropic:
    key = ANTHROPIC_KEY_PATH.read_text().strip()
    return anthropic.Anthropic(api_key=key)


_SCRIPT_SYSTEM_PROMPT = """\
You are the writer for the Odd Essentials weekly AI-news video podcast.

The show: a short (~3 minute) animated video commentary on developments in
the AI agent ecosystem, voiced by anthropomorphic crustacean hosts. Each
episode aggregates the editorial briefs of recent days into a tight,
two-host conversation that an attentive listener can follow without prior
context.

Tone: dry, observational, lightly skeptical, never breathless. Avoid hype
language. Avoid filler ("Welcome back", "Today on the show"). Treat the
listener as someone who already follows the space. The hosts trade
observations, push back on each other, and occasionally land a wry
crustacean-flavored aside — but the comedy serves the editorial, not the
other way around.

Rules:

- Output via the submit_episode_script tool. Never speak in plain prose.
- Use only the speakers given in the tool schema enum. The first speaker
  in any cold open is the anchor.
- Each segment is one continuous spoken line by one speaker — keep it
  conversational, 12 to 45 words. Hand off naturally.
- 8 to 16 segments total. Aim for the middle of that range.
- The episode title is for YouTube — concrete, ≤80 chars, no clickbait.
- The episode description is for YouTube — 100 to 500 chars, plain prose,
  no hashtags, no emojis, no links.
- delivery_note is optional and never spoken — use it sparingly to flag
  tone shifts the actor (TTS) might miss.
- Treat each brief item as source material to draw on, not a checklist to
  recite. Synthesis is the job; bullet-list narration is failure.
"""


def _render_corpus_for_prompt(corpus: list[BriefSummary]) -> str:
    parts: list[str] = []
    for b in corpus:
        parts.append(f"## Brief {b.id} — Issue {b.issue_no}: {b.title}")
        parts.append("")
        parts.append(f"_{b.dek}_")
        parts.append("")
        for it in b.items:
            headline = it.get("headline", "").strip()
            body = it.get("body", "").strip()
            parts.append(f"- **{headline}** {body}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _render_cast_for_prompt(cast: CastConfig) -> str:
    lines = ["Cast (use these speaker slugs in the tool call):"]
    for slug, m in cast.cast.items():
        anchor_marker = " [ANCHOR]" if slug == cast.anchor else ""
        lines.append(f"- `{slug}`{anchor_marker} — {m.display_name}, {m.role}: {m.persona}")
    return "\n".join(lines)


def _build_script_tool(allowed_speakers: list[str]) -> dict[str, Any]:
    return {
        "name": "submit_episode_script",
        "description": (
            "Submit the structured Episode 1 script. Single tool call, no "
            "follow-up. Output must respect every length and count bound; "
            "speakers must come from the enum."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "description", "segments"],
            "properties": {
                "title": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 80,
                    "description": "YouTube-facing episode title.",
                },
                "description": {
                    "type": "string",
                    "minLength": 100,
                    "maxLength": 500,
                    "description": "YouTube-facing episode description.",
                },
                "segments": {
                    "type": "array",
                    "minItems": 8,
                    "maxItems": 16,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["speaker", "text"],
                        "properties": {
                            "speaker": {"type": "string", "enum": allowed_speakers},
                            "text": {
                                "type": "string",
                                "description": (
                                    "One continuous spoken line, 12-45 words, "
                                    "conversational."
                                ),
                            },
                            "delivery_note": {
                                "type": ["string", "null"],
                                "maxLength": 200,
                            },
                        },
                    },
                },
            },
        },
    }


def generate_episode_script(
    corpus: list[BriefSummary],
    cast: CastConfig,
    *,
    model: str = SCRIPT_MODEL,
    max_attempts: int = 2,
) -> EpisodeScript:
    """Drive a single Anthropic tool-use call to produce a structured script.

    Retries once on schema validation failure with the validation error
    appended as a follow-up user message. After max_attempts, raises.
    """
    client = _anthropic_client()
    tool = _build_script_tool(cast.slugs())
    user = (
        _render_cast_for_prompt(cast)
        + "\n\nSource briefs (chronological):\n\n"
        + _render_corpus_for_prompt(corpus)
        + "\nProduce the Episode 1 script now via submit_episode_script."
    )

    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    last_err: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        resp = client.messages.create(
            model=model,
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": _SCRIPT_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[tool],
            tool_choice={"type": "tool", "name": "submit_episode_script"},
            messages=messages,
        )
        tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
        if tool_use is None:
            raise RuntimeError(
                f"model {model} did not invoke submit_episode_script "
                f"(stop_reason={resp.stop_reason})"
            )
        try:
            return EpisodeScript.model_validate(tool_use.input)
        except ValidationError as e:
            last_err = e
            if attempt >= max_attempts:
                break
            messages = [
                {"role": "user", "content": user},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_use.id,
                            "name": tool_use.name,
                            "input": tool_use.input,
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "is_error": True,
                            "content": (
                                f"Schema validation failed: {e}. "
                                "Resubmit a corrected script via the tool. "
                                "Pay close attention to the segment word-count "
                                "bounds (12-45) and the segments min/max counts (8-16)."
                            ),
                        }
                    ],
                },
            ]
    raise RuntimeError(f"script generation failed schema after {max_attempts} attempt(s): {last_err}")


# ---- Episode + manifest ---------------------------------------------------

def episode_dir(episode_id: str) -> Path:
    return EPISODES_DIR / episode_id


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


def _atomic_write_text(path: Path, text: str) -> None:
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
    """Write the Phase 0b initial manifest after script generation succeeds.

    Subsequent pipeline phases (TTS, Hedra, stitch, upload) update the
    manifest in-place via atomic rewrites. The manifest is the canonical
    state machine for resume — everything outside `data/episodes/<id>/`
    derives from it.

    Refuses to clobber an existing manifest unless overwrite=True. This is
    the safe default: silently overwriting would erase per-segment pipeline
    state (audio_path, clip_asset_id, attempts) that downstream phases write
    after script generation.
    """
    manifest_path = episode_dir(episode_id) / "manifest.json"
    if manifest_path.exists() and not overwrite:
        raise FileExistsError(
            f"manifest already exists at {manifest_path}. Pass overwrite=True "
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
    manifest_path = episode_dir(episode_id) / "manifest.json"
    _atomic_write_text(manifest_path, json.dumps(manifest, indent=2) + "\n")
    return manifest_path


# ---- Key loaders -----------------------------------------------------------

@lru_cache(maxsize=1)
def _load_podcast_keys_text() -> str:
    return PODCAST_KEYS_FILE.read_text()


def load_elevenlabs_key() -> str:
    m = re.search(r"^Elevenlabs key:\s*(\S+)", _load_podcast_keys_text(), flags=re.MULTILINE | re.IGNORECASE)
    if not m:
        raise SystemExit("ElevenLabs key not found in .keys.")
    return m.group(1).strip()


def load_hedra_key() -> str:
    m = re.search(r"^Hedra Key:\s*(\S+)", _load_podcast_keys_text(), flags=re.MULTILINE | re.IGNORECASE)
    if not m:
        raise SystemExit("Hedra key not found in .keys.")
    return m.group(1).strip()


# ---- Manifest update -------------------------------------------------------

def _read_manifest(manifest_path: Path) -> dict[str, Any]:
    return json.loads(manifest_path.read_text())


def _write_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    _atomic_write_text(manifest_path, json.dumps(manifest, indent=2) + "\n")


_MANIFEST_LOCK = threading.Lock()


def update_segment_state(manifest_path: Path, idx: int, **fields: Any) -> dict[str, Any]:
    with _MANIFEST_LOCK:
        manifest = _read_manifest(manifest_path)
        seg = manifest["segments"][idx]
        seg.update(fields)
        _write_manifest(manifest_path, manifest)
        return seg


# ---- ffprobe / ffmpeg helpers ---------------------------------------------

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


# ---- ElevenLabs TTS --------------------------------------------------------

def _tts_request(text: str, voice_id: str, api_key: str) -> bytes:
    body = json.dumps({"text": text, "model_id": TTS_MODEL}).encode()
    req = urllib.request.Request(
        f"{ELEVENLABS_API_BASE}/text-to-speech/{voice_id}",
        data=body,
        method="POST",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def generate_tts(*, text: str, voice_id: str, out_path: Path, api_key: str) -> Path:
    audio = _tts_request(text, voice_id, api_key)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(audio)
    return out_path


# ---- Hedra clip generation -------------------------------------------------

def _hedra_session(api_key: str) -> requests.Session:
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


def poll_hedra_clip(s: requests.Session, gen_id: str, *, poll_interval_sec: float = 5.0) -> tuple[str, str]:
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


# ---- Per-segment processing + canary validation ---------------------------

def audio_dir(episode_id: str) -> Path:
    return episode_dir(episode_id) / "audio"


def clips_dir(episode_id: str) -> Path:
    return episode_dir(episode_id) / "clips"


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
    manifest = _read_manifest(manifest_path)
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


# ---- CLI ------------------------------------------------------------------

def cmd_show_corpus(args: argparse.Namespace) -> int:
    corpus = load_eligible_corpus()
    print(f"Episode 1 eligible corpus: {len(corpus)} brief(s)")
    print(_summarize_corpus(corpus))
    return 0


def cmd_generate_script(args: argparse.Namespace) -> int:
    corpus = load_eligible_corpus()
    if not corpus:
        print("no eligible corpus — refusing to generate", file=sys.stderr)
        return 2

    episode_id = args.episode_id
    manifest_path = episode_dir(episode_id) / "manifest.json"
    if manifest_path.exists() and not args.force:
        print(
            f"manifest already exists at {manifest_path}. "
            "Pass --force to overwrite (drops all per-segment pipeline state).",
            file=sys.stderr,
        )
        return 2

    cast = load_cast()
    print(f"Generating script (model={SCRIPT_MODEL}) over {len(corpus)} brief(s)...")
    script = generate_episode_script(corpus, cast)

    episode_no = args.episode_no or derive_episode_no()
    run_date = args.run_date or _date.today().isoformat()
    manifest_path = write_initial_manifest(
        episode_id=episode_id,
        episode_no=episode_no,
        run_date=run_date,
        corpus=corpus,
        cast=cast,
        script=script,
        overwrite=args.force,
    )
    print(f"Script generated: {len(script.segments)} segments, title={script.title!r}")
    print(f"Hosts: {derive_hosts(script, cast)}")
    print(f"Manifest: {manifest_path}")
    return 0


EPISODE_DURATION_MIN_SEC = 60.0
EPISODE_DURATION_MAX_SEC = 360.0
STITCH_DURATION_TOLERANCE_SEC = 2.0

YOUTUBE_CATEGORY_ID = "28"           # Science & Technology
YOUTUBE_DEFAULT_LANGUAGE = "en"
YOUTUBE_DEFAULT_TAGS = ["AI agents", "Moltbook", "Odd Essentials"]
YOUTUBE_DISCLAIMER = (
    "\n\n---\nThis is AI-generated editorial commentary on agent-ecosystem "
    "activity. Hosts are synthetic; voices, animations, and narration are "
    "produced from a structured script. Not a record of human events."
)


def stitch_episode(*, manifest_path: Path, overwrite: bool = False) -> Path:
    """Concatenate per-segment clips into a single MP4 via ffmpeg concat demuxer.

    Re-encodes with fixed libx264/aac params (deterministic given the same
    inputs and same ffmpeg build). Stream copy would be faster but is
    fragile across slight Hedra clip variations.
    """
    manifest = _read_manifest(manifest_path)
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


def cmd_stitch(args: argparse.Namespace) -> int:
    eid = args.episode_id
    manifest_path = episode_dir(eid) / "manifest.json"
    if not manifest_path.exists():
        print(f"manifest missing at {manifest_path}", file=sys.stderr)
        return 2
    manifest = _read_manifest(manifest_path)

    expected_total = 0.0
    for s in manifest["segments"]:
        meta = ffprobe_streams(REPO_ROOT / s["clip_path"])
        expected_total += float(meta["format"]["duration"])

    print(f"Stitching {len(manifest['segments'])} clips (expected total ≈ {expected_total:.2f}s)...")
    final_path = stitch_episode(manifest_path=manifest_path, overwrite=args.force)
    print(f"Stitched: {final_path}")
    validate_stitched_output(final_path, expected_total)
    print("Validation OK")

    manifest = _read_manifest(manifest_path)
    manifest["stitched_path"] = str(final_path.relative_to(REPO_ROOT))
    manifest["validation_status"] = "stitched"
    _write_manifest(manifest_path, manifest)
    print("validation_status=stitched")
    return 0


# ---- SRT generation -------------------------------------------------------

def _format_srt_timestamp(sec: float) -> str:
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
    manifest = _read_manifest(manifest_path)
    eid = manifest["id"]
    segments = manifest["segments"]
    if any(s.get("audio_status") != "complete" or not s.get("audio_path") for s in segments):
        raise RuntimeError("not all segments have audio — refusing to build SRT")

    cues = []
    cursor = 0.0
    for i, seg in enumerate(segments):
        audio_meta = ffprobe_streams(REPO_ROOT / seg["audio_path"])
        dur = float(audio_meta["format"]["duration"])
        start = cursor
        end = cursor + dur
        cues.append(
            f"{i + 1}\n"
            f"{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}\n"
            f"{seg['text']}\n"
        )
        cursor = end

    srt_path = episode_dir(eid) / "captions.srt"
    srt_path.write_text("\n".join(cues), encoding="utf-8")
    return srt_path


# ---- YouTube upload -------------------------------------------------------

YOUTUBE_TOKEN_SECTION_HEADER = "# youtube_tokens"


def load_youtube_credentials():
    """Build a google.oauth2 Credentials from the refresh token in .keys."""
    from google.oauth2.credentials import Credentials  # local import — heavy SDK
    text = _load_podcast_keys_text()
    if YOUTUBE_TOKEN_SECTION_HEADER not in text:
        raise SystemExit(
            "YouTube tokens not found in .keys. Run scripts/youtube_consent.py first."
        )
    section = text.split(YOUTUBE_TOKEN_SECTION_HEADER, 1)[1].strip()
    payload = json.loads(section)
    return Credentials(
        token=None,
        refresh_token=payload["refresh_token"],
        client_id=payload["client_id"],
        client_secret=payload["client_secret"],
        token_uri=payload["token_uri"],
        scopes=payload.get("scopes"),
    )


def upload_youtube_video(
    *,
    credentials,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    visibility: str = DEFAULT_VISIBILITY,
) -> str:
    """Resumable upload to YouTube. Returns the videoId."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": YOUTUBE_CATEGORY_ID,
            "defaultLanguage": YOUTUBE_DEFAULT_LANGUAGE,
            "defaultAudioLanguage": YOUTUBE_DEFAULT_LANGUAGE,
        },
        "status": {
            "privacyStatus": visibility,
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
        },
    }
    media = MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"    upload progress: {int(status.progress() * 100)}%")
    return response["id"]


def upload_youtube_caption(*, credentials, video_id: str, srt_path: Path, language: str = YOUTUBE_DEFAULT_LANGUAGE) -> str:
    """Upload an SRT as a caption track for `video_id`. Returns the caption id."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    body = {
        "snippet": {
            "videoId": video_id,
            "language": language,
            "name": "English",
            "isDraft": False,
        }
    }
    media = MediaFileUpload(str(srt_path), mimetype="application/octet-stream", resumable=False)
    resp = youtube.captions().insert(part="snippet", body=body, media_body=media).execute()
    return resp["id"]


def verify_youtube_video(*, credentials, video_id: str) -> dict[str, Any]:
    """Confirm the upload via videos.list. Returns the snippet+status payload."""
    from googleapiclient.discovery import build
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    resp = youtube.videos().list(part="id,snippet,status", id=video_id).execute()
    items = resp.get("items", [])
    if not items:
        raise RuntimeError(f"videos.list found no item for id={video_id}")
    return items[0]


def cmd_upload(args: argparse.Namespace) -> int:
    eid = args.episode_id
    manifest_path = episode_dir(eid) / "manifest.json"
    if not manifest_path.exists():
        print(f"manifest missing at {manifest_path}", file=sys.stderr)
        return 2
    manifest = _read_manifest(manifest_path)
    if not manifest.get("stitched_path"):
        print("stitched_path missing — run stitch first.", file=sys.stderr)
        return 2

    final_path = REPO_ROOT / manifest["stitched_path"]
    if not final_path.exists():
        print(f"stitched file missing: {final_path}", file=sys.stderr)
        return 2

    print("Generating SRT from segment timing...")
    srt_path = generate_srt(manifest_path=manifest_path)
    print(f"  SRT: {srt_path} ({srt_path.stat().st_size} bytes)")

    print("Loading YouTube credentials...")
    creds = load_youtube_credentials()

    visibility = manifest.get("visibility", DEFAULT_VISIBILITY)
    video_id = manifest.get("youtube_id")

    if video_id:
        print(f"manifest already records youtube_id={video_id!r}; skipping video upload.")
        record = verify_youtube_video(credentials=creds, video_id=video_id)
        print(f"  privacyStatus: {record['status']['privacyStatus']!r}")
        print(f"  uploadStatus:  {record['status'].get('uploadStatus')!r}")
        if record["status"]["privacyStatus"] != visibility:
            raise RuntimeError(
                f"privacyStatus on YouTube ({record['status']['privacyStatus']!r}) "
                f"does not match requested {visibility!r}"
            )
    else:
        title = manifest["script"]["title"]
        description = manifest["script"]["description"] + YOUTUBE_DISCLAIMER
        print(f"Uploading video to YouTube ({visibility}): title={title!r}")
        video_id = upload_youtube_video(
            credentials=creds,
            video_path=final_path,
            title=title,
            description=description,
            tags=YOUTUBE_DEFAULT_TAGS,
            visibility=visibility,
        )
        print(f"  videoId: {video_id}")
        manifest = _read_manifest(manifest_path)
        manifest["youtube_id"] = video_id
        manifest["validation_status"] = "video_uploaded"
        _write_manifest(manifest_path, manifest)

        print("Verifying via videos.list...")
        record = verify_youtube_video(credentials=creds, video_id=video_id)
        print(f"  privacyStatus: {record['status']['privacyStatus']!r}")
        print(f"  uploadStatus:  {record['status'].get('uploadStatus')!r}")
        if record["status"]["privacyStatus"] != visibility:
            raise RuntimeError(
                f"privacyStatus on YouTube ({record['status']['privacyStatus']!r}) "
                f"does not match requested {visibility!r}"
            )

    if manifest.get("youtube_caption_id"):
        print(f"manifest already records youtube_caption_id; skipping caption upload.")
    else:
        print("Uploading caption track...")
        caption_id = upload_youtube_caption(credentials=creds, video_id=video_id, srt_path=srt_path)
        print(f"  caption id: {caption_id}")
        manifest = _read_manifest(manifest_path)
        manifest["youtube_caption_id"] = caption_id
        _write_manifest(manifest_path, manifest)

    manifest = _read_manifest(manifest_path)
    cast = load_cast()
    final_meta = ffprobe_streams(REPO_ROOT / manifest["stitched_path"])
    duration_min = max(1, int(round(float(final_meta["format"]["duration"]) / 60.0)))
    record = EpisodeRecord(
        id=eid,
        episodeNo=int(manifest["episode_no"]),
        title=manifest["script"]["title"],
        date=manifest["run_date"],
        durationMinutes=duration_min,
        youtubeId=manifest["youtube_id"],
        description=manifest["script"]["description"],
        hosts=derive_hosts(EpisodeScript.model_validate(manifest["script"]), cast),
    )
    manifest["episode_record"] = record.model_dump()
    manifest["validation_status"] = "uploaded"
    _write_manifest(manifest_path, manifest)
    print("validation_status=uploaded")
    print(f"Episode record (matches SPA Episode shape): {record.model_dump()}")
    print(f"YouTube URL: https://www.youtube.com/watch?v={video_id}")
    return 0


def cmd_produce_segments(args: argparse.Namespace) -> int:
    """Canary-then-scale TTS + Hedra clip generation across all manifest segments.

    Segment 0 runs as the canary. If its objective gates pass, segments
    1..N-1 run with the same gates. Any segment failure aborts the run
    with the manifest left in whatever partial state it reached — the
    next invocation can reuse already-complete segments.
    """
    eid = args.episode_id
    manifest_path = episode_dir(eid) / "manifest.json"
    if not manifest_path.exists():
        print(f"manifest missing at {manifest_path} — run generate-script first.", file=sys.stderr)
        return 2

    manifest = _read_manifest(manifest_path)
    segments = manifest["segments"]
    n = len(segments)
    print(f"Producing {n} segment(s) for episode {eid}")

    cast = load_cast()
    e_key = load_elevenlabs_key()
    h_session = _hedra_session(load_hedra_key())

    print("Canary: segment 0...")
    process_segment(manifest_path=manifest_path, idx=0, cast=cast, elevenlabs_key=e_key, hedra_session=h_session)
    print("Canary green. Scaling to remaining segments in parallel...")

    parallel_workers = min(args.parallel, max(1, n - 1))
    if parallel_workers <= 1 or n <= 2:
        for idx in range(1, n):
            process_segment(
                manifest_path=manifest_path, idx=idx, cast=cast,
                elevenlabs_key=e_key, hedra_session=h_session,
            )
    else:
        with ThreadPoolExecutor(max_workers=parallel_workers) as ex:
            futures = {
                ex.submit(
                    process_segment,
                    manifest_path=manifest_path, idx=idx, cast=cast,
                    elevenlabs_key=e_key, hedra_session=h_session,
                ): idx
                for idx in range(1, n)
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    print(f"  seg{idx:02d}: FAILED — {e}", file=sys.stderr)
                    raise

    manifest = _read_manifest(manifest_path)
    manifest["validation_status"] = "segments_complete"
    _write_manifest(manifest_path, manifest)
    print(f"All {n} segments complete. validation_status=segments_complete")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Podcast orchestrator (Phase 0b).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show-corpus", help="Print eligible Episode 1 corpus and exit.")

    p_gen = sub.add_parser(
        "generate-script",
        help="Generate the Episode 1 script and write the initial manifest.",
    )
    p_gen.add_argument("--episode-id", default="ep-001")
    p_gen.add_argument("--episode-no", type=int, default=None)
    p_gen.add_argument("--run-date", default=None, help="ISO date; defaults to today.")
    p_gen.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing manifest. Drops all per-segment pipeline state.",
    )

    p_prod = sub.add_parser(
        "produce-segments",
        help="Canary-then-scale TTS + Hedra clip generation for all manifest segments.",
    )
    p_prod.add_argument("--episode-id", default="ep-001")
    p_prod.add_argument(
        "--parallel",
        type=int,
        default=4,
        help="Parallel workers for non-canary segments (default 4).",
    )

    p_stitch = sub.add_parser(
        "stitch",
        help="Concat per-segment clips into final.mp4 + ffprobe validate.",
    )
    p_stitch.add_argument("--episode-id", default="ep-001")
    p_stitch.add_argument("--force", action="store_true", help="Overwrite final.mp4 if it exists.")

    p_up = sub.add_parser(
        "upload",
        help="Generate SRT, upload final.mp4 to YouTube unlisted, verify, upload captions.",
    )
    p_up.add_argument("--episode-id", default="ep-001")

    args = parser.parse_args(argv)
    if args.cmd == "show-corpus":
        return cmd_show_corpus(args)
    if args.cmd == "generate-script":
        return cmd_generate_script(args)
    if args.cmd == "produce-segments":
        return cmd_produce_segments(args)
    if args.cmd == "stitch":
        return cmd_stitch(args)
    if args.cmd == "upload":
        return cmd_upload(args)
    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
