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

import anthropic
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
) -> Path:
    """Write the Phase 0b initial manifest after script generation succeeds.

    Subsequent pipeline phases (TTS, Hedra, stitch, upload) update the
    manifest in-place via atomic rewrites. The manifest is the canonical
    state machine for resume — everything outside `data/episodes/<id>/`
    derives from it.
    """
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
    cast = load_cast()
    print(f"Generating script (model={SCRIPT_MODEL}) over {len(corpus)} brief(s)...")
    script = generate_episode_script(corpus, cast)

    episode_id = args.episode_id
    episode_no = args.episode_no or derive_episode_no()
    run_date = args.run_date or _date.today().isoformat()
    manifest_path = write_initial_manifest(
        episode_id=episode_id,
        episode_no=episode_no,
        run_date=run_date,
        corpus=corpus,
        cast=cast,
        script=script,
    )
    print(f"Script generated: {len(script.segments)} segments, title={script.title!r}")
    print(f"Hosts: {derive_hosts(script, cast)}")
    print(f"Manifest: {manifest_path}")
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

    args = parser.parse_args(argv)
    if args.cmd == "show-corpus":
        return cmd_show_corpus(args)
    if args.cmd == "generate-script":
        return cmd_generate_script(args)
    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
