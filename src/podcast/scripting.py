"""Anthropic tool-use call that produces a validated EpisodeScript.

System prompt is cache-controlled (ephemeral) so steady-state weekly runs
amortise it. Output enforcement is via the tool input_schema with the cast
slugs supplied as the speaker enum, so the model can't emit speakers
outside the cast contract. One Pydantic-validation retry on schema
mismatch with the validation error returned to the model as tool_result.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import anthropic
from pydantic import ValidationError

from .config import SCRIPT_MODEL
from .keys import load_anthropic_key
from .schema import BriefSummary, CastConfig, EpisodeScript


@lru_cache(maxsize=1)
def _anthropic_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=load_anthropic_key())


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
    appended as a follow-up tool_result message. After max_attempts, raises.
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
