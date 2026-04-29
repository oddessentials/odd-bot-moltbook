"""Pydantic models + the lightweight BriefSummary dataclass.

Every cross-module data shape that the engine produces or consumes has its
canonical definition here. The Pydantic models also drive the Anthropic
tool input_schema (see scripting.py) and the post-upload Episode shape
validation (see cli.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field, field_validator


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
    episodes.json write until Phase 2.

    `id` is pattern-constrained to slug-safe characters: it appears in
    the canonical URL, in the OG meta tags, in data/episodes.json, and
    will appear in the Phase 3 X-post. A non-slug id (`..`, embedded
    quotes, spaces, html-significant characters) would let an attacker-
    crafted record either escape the OG tag's `content="..."` attribute
    or construct a path-traversal canonical URL. The constraint enforces
    the safe subset at the single point every consumer goes through."""

    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
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
