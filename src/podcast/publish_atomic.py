"""Atomic Phase 6 + Phase 7 with worktree-rollback on failure.

Background: PR #21 added Phase 7 (`rebuild_spa_and_verify`) after Phase 6
(`cmd_publish`). Phase 6 mutates two load-bearing files —
`data/episodes.json` (tracked, allowlisted) and the per-episode
`manifest.json` (gitignored, drives orchestrator phase-skipping). Phase 7
runs Vite + a hard verification gate that scans the rebuilt
`docs/assets/index-*.js` chunks for the new episode's `id` and
`youtubeId`. A Phase 7 failure after Phase 6 success leaves disk state
where:

  - `data/episodes.json` shows the new episode (committed-state divergence
    with `origin/main`).
  - Manifest sits at `validation_status="published"` even though the
    public publish artifacts never reached origin.
  - `docs/` may be partially or fully rebuilt with new content-hashed
    Vite assets, leaving the worktree dirty so the wrapper's next-fire
    `src.git_sync.reconcile_with_origin` halts.

This module makes the Phase 6 + Phase 7 sequence atomic from the
wrapper's perspective:

  - On full success: state mutations persist, wrapper commits.
  - On any failure inside Phase 6 or Phase 7: tracked publish artifacts
    return to their pre-cmd_run committed state (`git restore` +
    `git clean -fdq -- docs`) and the manifest reverts to its pre-publish
    bytes. The wrapper then exits non-zero via `set -e`, with the
    worktree clean for the next retry.

Scope discipline: this module does NOT change `publish_episode`'s
internals, the ratchet behavior of `advance_validation_status`, or
the daily wrapper. It is a thin, testable atomic wrapper around two
existing functions.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .config import REPO_ROOT
from .episodes import cmd_publish
from .spa_rebuild import rebuild_spa_and_verify


def atomic_publish_and_verify(
    *,
    episode_id: str,
    manifest_path: Path,
    repo_root: Path | None = None,
) -> int:
    """Run Phase 6 (`cmd_publish`) then Phase 7 (`rebuild_spa_and_verify`)
    as a single atomic step.

    Returns `cmd_publish`'s status code on Phase 6 failure (gates raise
    `PublishGateError` inside `publish_episode` *before* any write, so
    no rollback is needed in that path — verified by reading
    `publish_episode` source).

    Returns 0 on full success.

    Raises whatever Phase 7 raises after rolling back the worktree to
    its pre-cmd_run committed state plus the manifest to its
    pre-publish bytes. The wrapper relies on this raise + `set -e` to
    refuse the publish commit and surface the failure to the operator.

    `repo_root` is overridable so the test sandbox can swap in a temp
    git repo without monkeypatching module constants.
    """
    actual_root = repo_root if repo_root is not None else REPO_ROOT

    manifest_snap = manifest_path.read_bytes()

    rc = cmd_publish(argparse.Namespace(episode_id=episode_id))
    if rc != 0:
        return rc

    try:
        rebuild_spa_and_verify(episode_id=episode_id)
    except Exception:
        try:
            _rollback_publish_state(
                repo_root=actual_root,
                manifest_path=manifest_path,
                manifest_snap=manifest_snap,
            )
        except Exception as rb_err:
            # Rollback failures must NOT mask the original Phase 7
            # failure — that's what the wrapper acts on. Surface the
            # rollback failure to the log; let the original raise.
            print(
                f"WARN: rollback after Phase 7 failure also failed: "
                f"{rb_err!r}. Worktree may be dirty; operator must "
                f"manually clean before retry.",
                file=sys.stderr,
            )
        raise
    return 0


def _rollback_publish_state(
    *,
    repo_root: Path,
    manifest_path: Path,
    manifest_snap: bytes,
) -> None:
    """Restore the worktree to its pre-cmd_run state.

    Three categories, three mechanics:

      - `data/episodes.json` (tracked, modified by `publish_episode`):
        `git restore` returns the file to the HEAD-tracked content.
      - `docs/` (tracked tree mutated by `cmd_og` and `_run_build`):
        `git restore -- docs` restores modified + deleted tracked files;
        `git clean -fdq -- docs` removes untracked artifacts (e.g.,
        Vite-generated content-hashed asset chunks like
        `docs/assets/index-NEWHASH.js`).
      - Manifest (gitignored, drives phase-skipping): byte-restore from
        the pre-Phase-6 snapshot.

    No-LLM, no-network. Subprocess errors propagate to the caller; the
    caller is responsible for logging and not masking the originating
    exception.
    """
    subprocess.run(
        ["git", "restore", "--", "data/episodes.json", "docs"],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clean", "-fdq", "--", "docs"],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
    )
    manifest_path.write_bytes(manifest_snap)
