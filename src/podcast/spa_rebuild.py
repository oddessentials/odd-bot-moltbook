"""SPA rebuild + hard verification gate for the podcast publish path.

The SPA hydrates its episode list from `data/episodes.json` at Vite
build time — `agent-brief/client/src/hooks/useEpisodes.ts:15` imports
the file as a build-time module, no runtime fetch. Publishing a new
episode mutates `data/episodes.json` but does NOT regenerate the
bundle; without a rebuild, the consumer-side `/podcast` listing +
homepage card grid stay stale until the next daily build (~7h).

This module closes that loop and gates the podcast publish on a
deterministic local artifact check:

  1. `rebuild_spa(...)` reuses `src.publish._run_build` — the same
     helper the daily pipeline runs — so the build behavior stays
     defined in one place.

  2. `verify_bundle_contains_episode(...)` scans the rebuilt
     `docs/assets/index-*.js` for the new episode's `id` and
     `youtubeId`. Raises `RuntimeError` if either marker is missing.

The composed `rebuild_spa_and_verify(...)` is the orchestrator Phase 7
entry point. No LLM, no network, no manifest mutation.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import REPO_ROOT

DATA_DIR = REPO_ROOT / "data"
DOCS_ASSETS_DIR = REPO_ROOT / "docs" / "assets"
EPISODES_PATH = DATA_DIR / "episodes.json"


def _load_episode_record(
    episode_id: str, episodes_path: Path = EPISODES_PATH,
) -> dict:
    """Read `episodes_path` and return the entry whose `id` matches.

    Raises `RuntimeError` if the file does not exist or the id is
    absent — Phase 7 must not silently skip the verification gate
    when its input is corrupt.
    """
    if not episodes_path.exists():
        raise RuntimeError(f"no episodes.json at {episodes_path}")
    entries = json.loads(episodes_path.read_text())
    for entry in entries:
        if entry.get("id") == episode_id:
            return entry
    raise RuntimeError(
        f"episode {episode_id!r} not present in {episodes_path} — "
        f"cmd_publish must run before SPA rebuild verification"
    )


def verify_bundle_contains_episode(
    *,
    episode: dict,
    docs_assets_dir: Path = DOCS_ASSETS_DIR,
) -> None:
    """Hard gate: rebuilt SPA bundle must contain the new episode's
    `id` AND `youtubeId`. Raises `RuntimeError` if either is missing.

    Scans every `index-*.js` chunk under `docs_assets_dir`. Vite
    content-hashes the filename so we don't pre-compute the hash; we
    grep across all chunks instead.
    """
    js_files = sorted(docs_assets_dir.glob("index-*.js"))
    if not js_files:
        raise RuntimeError(
            f"no JS bundle under {docs_assets_dir} — Vite output "
            f"missing; _run_build did not complete"
        )
    bundle = "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in js_files
    )
    eid = episode.get("id")
    ytid = episode.get("youtubeId")
    missing: list[str] = []
    if not eid or eid not in bundle:
        missing.append(f"id={eid!r}")
    if not ytid or ytid not in bundle:
        missing.append(f"youtubeId={ytid!r}")
    if missing:
        raise RuntimeError(
            f"SPA bundle missing episode markers: {', '.join(missing)}. "
            f"Refusing to let the publish commit ship a stale consumer "
            f"surface. Scanned: {[p.name for p in js_files]}"
        )


def rebuild_spa_and_verify(*, episode_id: str) -> None:
    """Phase 7 of the podcast publish: regenerate the SPA bundle so the
    build-time-snapshot of `data/episodes.json` matches what
    `cmd_publish` just wrote, then verify the new episode's `id` and
    `youtubeId` are baked into the JS bundle.

    Raises on build failure or verification miss. The wrapper relies
    on `set -e` to refuse the publish commit when this raises.
    """
    # Lazy import to keep podcast-side tests free of daily-pipeline
    # transitive imports (anthropic, summarize, etc.).
    from src.publish import _load_briefs, _run_build

    episode = _load_episode_record(episode_id)
    _run_build(time.time(), _load_briefs())
    verify_bundle_contains_episode(episode=episode)
