"""Reconcile local main with origin/main before automation does work.

Used by the daily orchestrator (`src.publish.run_daily_publish`) and the
weekly podcast wrapper (`scripts/run-weekly-podcast.sh`) at the start of
each run, BEFORE any new content generation, commit, or push. Closes the
2026-05-02 race where the prior day's x-post sidecar advanced origin and
the next morning's daily wrapper committed on stale local HEAD, leaving
local diverged 1↔1 from origin and halting every subsequent run.

Contract:

  - Clean-worktree precondition. Tracked-file modifications or staged
    changes → halt:dirty-worktree. Refuses to fetch/merge/rebase/push
    over operator WIP.
  - `git fetch origin <branch>`. Failure → halt:fetch-failed.
  - Compare HEAD to origin/<branch>:
      (0, 0)              → ok:noop
      (0, behind)         → ff merge → ok:fast-forward
      (ahead, 0)          → push → ok:push or halt:push-failed
      (ahead, behind):
        all bot-owned     → rebase → push → ok:rebase or halt:push-failed
                            rebase conflict → halt:diverged:rebase-conflict
        any non-bot-owned → halt:diverged

Bot-owned predicate (verified against repo history 2026-04-27 → 2026-05-03,
12 commits, 100% author-name consistency):
  - Author name == "odd-bot"
  - Subject matches ^chore\\((publish|x-post|podcast|podcast-x-post)\\):

Email is NOT used: author email is split between admin@oddessentials.com
(commits made by the local launchd runtime, inheriting repo git config)
and odd-bot@oddessentials.ai (commits made by GitHub Actions sidecar
workflows, set explicitly via `git config`). Both are bot-owned, so the
predicate gates on author-name + subject, never email.

CLI form (used by the weekly bash wrapper):

    python -m src.git_sync reconcile [--branch main]

Stdout contract: human-readable diagnostic lines, terminated by exactly
one parseable status line:

    STATUS:ok:<action>:<detail>
    STATUS:halt:<reason>:<detail>

Exit code: 0 on ok, 1 on halt.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass


BOT_AUTHOR_NAME = "odd-bot"
BOT_SUBJECT_RE = re.compile(r"^chore\((publish|x-post|podcast|podcast-x-post)\):")


@dataclass(frozen=True)
class ReconcileResult:
    status: str
    action: str
    ahead: int = 0
    behind: int = 0
    detail: str = ""

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"

    def status_line(self) -> str:
        parts = ["STATUS", self.status, self.action]
        if self.detail:
            parts.append(self.detail)
        return ":".join(parts)


def _git(
    *args: str, check: bool = True, cwd: str | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        check=check,
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def _is_worktree_clean(cwd: str | None = None) -> bool:
    """Tracked-file cleanliness check.

    Untracked files (status `??`) are NOT a halt condition — they
    survive rebase/fast-forward and don't risk operator data loss.
    Anything else (modified, staged, deleted, renamed, conflicted)
    is treated as dirty.
    """
    proc = _git("status", "--porcelain", cwd=cwd)
    for line in proc.stdout.splitlines():
        if not line.startswith("??"):
            return False
    return True


def _ahead_behind(branch: str, cwd: str | None = None) -> tuple[int, int]:
    proc = _git(
        "rev-list", "--left-right", "--count", f"HEAD...origin/{branch}", cwd=cwd
    )
    ahead_s, behind_s = proc.stdout.strip().split()
    return int(ahead_s), int(behind_s)


def _local_only_commits(branch: str, cwd: str | None = None) -> list[str]:
    proc = _git("rev-list", f"origin/{branch}..HEAD", cwd=cwd)
    return [line for line in proc.stdout.strip().split("\n") if line]


def _is_bot_owned(commit_sha: str, cwd: str | None = None) -> bool:
    proc = _git("log", "-1", "--pretty=%an%n%s", commit_sha, cwd=cwd)
    parts = proc.stdout.split("\n", 1)
    if len(parts) < 2:
        return False
    author = parts[0]
    subject = parts[1].rstrip("\n")
    return author == BOT_AUTHOR_NAME and bool(BOT_SUBJECT_RE.match(subject))


def _classify_push_failure(stderr: str) -> str:
    err = stderr.lower()
    if "non-fast-forward" in err or "rejected" in err or "fetch first" in err:
        return "deferred-conflict"
    if (
        "authentication" in err
        or "permission denied" in err
        or "could not read" in err
    ):
        return "deferred-auth"
    return "deferred-network"


def _classify_fetch_failure(stderr: str) -> str:
    err = stderr.lower()
    if (
        "could not resolve" in err
        or "connection" in err
        or "network" in err
        or "timed out" in err
    ):
        return "network"
    if (
        "authentication" in err
        or "permission denied" in err
        or "could not read" in err
    ):
        return "auth"
    return "unknown"


def _try_push(branch: str, cwd: str | None = None) -> tuple[bool, str]:
    proc = _git("push", "origin", branch, check=False, cwd=cwd)
    if proc.returncode == 0:
        return True, "ok"
    return False, _classify_push_failure(proc.stderr or "")


def reconcile_with_origin(
    branch: str = "main", cwd: str | None = None
) -> ReconcileResult:
    """Reconcile HEAD with origin/<branch> per the module contract.

    Returns a ReconcileResult; never raises on git operation failure
    (those become halt:* statuses). Raises only on subprocess setup
    errors (git binary missing, etc.) or on logically-impossible states
    like rev-list output that can't be parsed — both of which are bugs,
    not runtime conditions.
    """
    # 1. Clean-worktree precondition. Protects operator WIP from being
    #    fast-forwarded or rebased over. Daily/weekly wrappers run from
    #    a clean tree by contract; if dirty, something upstream is
    #    wrong and the operator should investigate before automation
    #    proceeds.
    if not _is_worktree_clean(cwd=cwd):
        return ReconcileResult(
            status="halt",
            action="dirty-worktree",
            detail="tracked-changes-present",
        )

    # 2. Fetch. Without this the ahead/behind compare reads the local
    #    cache of origin/<branch>, which can lag actual origin by
    #    however long since the last fetch.
    fetch = _git("fetch", "origin", branch, check=False, cwd=cwd)
    if fetch.returncode != 0:
        return ReconcileResult(
            status="halt",
            action="fetch-failed",
            detail=_classify_fetch_failure(fetch.stderr or ""),
        )

    # 3. Compute divergence.
    ahead, behind = _ahead_behind(branch, cwd=cwd)

    # 4. (0, 0) — clean.
    if ahead == 0 and behind == 0:
        return ReconcileResult(status="ok", action="noop")

    # 5. (0, N) — fast-forward.
    if ahead == 0 and behind > 0:
        ff = _git("merge", "--ff-only", f"origin/{branch}", check=False, cwd=cwd)
        if ff.returncode != 0:
            return ReconcileResult(
                status="halt",
                action="fast-forward-failed",
                ahead=ahead,
                behind=behind,
                detail=(ff.stderr or "").strip().split("\n")[0][:120],
            )
        return ReconcileResult(
            status="ok", action="fast-forward", ahead=0, behind=behind
        )

    # 6. (N, 0) — push.
    if ahead > 0 and behind == 0:
        ok, classification = _try_push(branch, cwd=cwd)
        if not ok:
            return ReconcileResult(
                status="halt",
                action="push-failed",
                ahead=ahead,
                detail=classification,
            )
        return ReconcileResult(status="ok", action="push", ahead=ahead)

    # 7. (N, M) — diverged. Auto-rebase ONLY if every local-only commit
    #    is bot-owned. Any non-bot-owned commit (operator hand-commit,
    #    feature-branch artifact, anything we don't recognize) is a
    #    refusal-to-decide signal.
    locals_ = _local_only_commits(branch, cwd=cwd)
    non_bot = [c for c in locals_ if not _is_bot_owned(c, cwd=cwd)]
    if non_bot:
        return ReconcileResult(
            status="halt",
            action="diverged",
            ahead=ahead,
            behind=behind,
            detail=f"{len(non_bot)}-non-bot-of-{len(locals_)}",
        )

    rb = _git("rebase", f"origin/{branch}", check=False, cwd=cwd)
    if rb.returncode != 0:
        # Conflict on bot-owned commits — should be vanishingly rare
        # (the bot commits touch disjoint file sets across types) but
        # if it happens we abort cleanly and let the operator resolve.
        _git("rebase", "--abort", check=False, cwd=cwd)
        return ReconcileResult(
            status="halt",
            action="diverged",
            ahead=ahead,
            behind=behind,
            detail="rebase-conflict",
        )
    ok, classification = _try_push(branch, cwd=cwd)
    if not ok:
        return ReconcileResult(
            status="halt",
            action="rebase-then-push-failed",
            ahead=ahead,
            behind=behind,
            detail=classification,
        )
    return ReconcileResult(
        status="ok", action="rebase", ahead=ahead, behind=behind
    )


def _print_human_log(result: ReconcileResult) -> None:
    """Emit human-readable diagnostic lines for the run log.

    Mirrors the existing weekly-wrapper log style ("  fetch: ...",
    "  pre-flight: ..."). Bash captures stdout into the log file;
    the operator scans these when investigating.
    """
    if result.status == "ok":
        if result.action == "noop":
            print("reconcile: 0 ahead / 0 behind — clean, no action needed")
        elif result.action == "fast-forward":
            print(
                f"reconcile: 0 ahead / {result.behind} behind — fast-forwarded "
                "to origin (likely the prior run's x-post sidecar)"
            )
        elif result.action == "push":
            print(
                f"reconcile: {result.ahead} ahead / 0 behind — pushed prior "
                "unpushed commit(s); downstream workflows fire on origin"
            )
        elif result.action == "rebase":
            print(
                f"reconcile: {result.ahead} ahead / {result.behind} behind — "
                "all local-only commits are bot-owned; rebased onto origin "
                "and pushed"
            )
    else:
        print(
            f"reconcile: HALT ({result.action}); ahead={result.ahead} "
            f"behind={result.behind} detail={result.detail}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(prog="src.git_sync")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_recon = sub.add_parser(
        "reconcile",
        help=(
            "reconcile local with origin before automation work. Halts on "
            "dirty worktree, fetch failure, push failure, or non-bot-owned "
            "divergence."
        ),
    )
    p_recon.add_argument("--branch", default="main")
    args = parser.parse_args()
    if args.cmd != "reconcile":
        return 1
    result = reconcile_with_origin(branch=args.branch)
    _print_human_log(result)
    print(result.status_line())
    return 0 if result.is_ok else 1


if __name__ == "__main__":
    sys.exit(main())
