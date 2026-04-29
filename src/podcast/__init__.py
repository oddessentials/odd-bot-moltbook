"""Podcast orchestrator package for the weekly AI-news video podcast.

This package holds the engine that consumes daily briefs and emits a
short two-host video podcast. Locked plan: `plans/podcast-pipeline.md`.

Pipeline shape:

    generate-script  -> produce-segments (canary + scale)
                     -> stitch
                     -> upload (YouTube unlisted + SRT caption track)

Module layout:

    config       — paths, model identifiers, validation gates (no behavior).
    schema       — Pydantic models (CastConfig, Segment, EpisodeScript,
                   EpisodeRecord) + BriefSummary dataclass.
    corpus       — eligible-brief loader from data/briefs.json.
    cast         — config/podcast-cast.yaml loader + cast_config_hash.
    keys         — secret + credential loaders (.keys, openclaw, OAuth).
    manifest     — episode manifest read/write/update + atomic writes +
                   episode-dir helpers.
    media        — ffprobe / ffmpeg helpers + SRT generation.
    scripting    — Anthropic tool-use script generation.
    tts          — ElevenLabs TTS for one segment at a time.
    hedra        — Hedra Character-3 audio upload + clip generation +
                   poll + download.
    segments     — process_segment + validate_segment_outputs (canary
                   gates).
    stitch       — ffmpeg concat + validate_stitched_output.
    youtube      — upload + caption + verify.
    cli          — argparse + cmd_* + main.

Invariants enforced across the engine (see plans/podcast-pipeline.md):

  - Identity-mapping stability: voice_id + hedra_image_asset_id are
    inputs read from the cast contract, never created/mutated at runtime.
  - Segment-level retry only — never episode-level.
  - YouTube upload is unlisted in Phase 0; the public flip is a separate
    publish-event concern handled in Phase 2.
  - No writes to data/briefs.json, data/x-posts.jsonl, the daily publish
    path, or the daily X-post workflow.
"""

from .cli import main

__all__ = ["main"]
