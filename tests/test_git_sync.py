"""Regression tests for src.git_sync.reconcile_with_origin.

Each test sets up a real bare-remote + local-clone pair in a tempdir
and exercises a specific divergence shape. No mocking of git — these
verify behavior against the actual git binary.

The 2026-05-02 race that motivated this module is covered explicitly:
  - TestReconcileFastForward: prior x-post sidecar landed on origin,
    next morning's wrapper starts stale.
  - TestReconcileRebase: same race, but local also made a new bot-
    owned publish commit on stale HEAD before reconciliation runs.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from src.git_sync import reconcile_with_origin


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


def _commit(
    cwd: Path,
    message: str,
    files: dict[str, str] | None = None,
    author_name: str = "odd-bot",
    author_email: str = "admin@oddessentials.com",
) -> None:
    if files:
        for path, content in files.items():
            full = cwd / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content)
            _git("add", path, cwd=cwd)
    _git(
        "-c", f"user.name={author_name}",
        "-c", f"user.email={author_email}",
        "commit", "-m", message,
        cwd=cwd,
    )


class ReconcileTestBase(unittest.TestCase):
    """Sets up a bare remote + local clone with one initial commit on main."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.remote_path = root / "remote.git"
        self.local_path = root / "local"

        # Bare remote.
        self.remote_path.mkdir()
        _git("init", "--bare", "-b", "main", cwd=self.remote_path)

        # Local repo: init, configure as the bot identity used by the
        # daily wrapper (admin@oddessentials.com), seed initial commit,
        # push.
        self.local_path.mkdir()
        _git("init", "-b", "main", cwd=self.local_path)
        for k, v in [
            ("user.name", "odd-bot"),
            ("user.email", "admin@oddessentials.com"),
            ("commit.gpgsign", "false"),
            ("tag.gpgsign", "false"),
        ]:
            _git("config", k, v, cwd=self.local_path)
        (self.local_path / "README.md").write_text("v0\n")
        _git("add", "README.md", cwd=self.local_path)
        _git("commit", "-m", "initial", cwd=self.local_path)
        _git("remote", "add", "origin", str(self.remote_path), cwd=self.local_path)
        _git("push", "-u", "origin", "main", cwd=self.local_path)

    def _advance_origin(
        self,
        message: str,
        files: dict[str, str] | None = None,
        author_name: str = "odd-bot",
        author_email: str = "odd-bot@oddessentials.ai",
    ) -> None:
        """Simulate something else (GH Actions x-post sidecar, operator
        manual push) advancing origin/main from outside the local clone.
        """
        root = Path(self._tmp.name)
        helper = root / "helper"
        if not helper.exists():
            _git("clone", str(self.remote_path), str(helper), cwd=root)
            _git("config", "commit.gpgsign", "false", cwd=helper)
        else:
            _git("pull", "origin", "main", cwd=helper)
        _commit(
            helper, message, files=files,
            author_name=author_name, author_email=author_email,
        )
        _git("push", "origin", "main", cwd=helper)

    def _local_commit(
        self,
        message: str,
        files: dict[str, str] | None = None,
        author_name: str = "odd-bot",
        author_email: str = "admin@oddessentials.com",
    ) -> None:
        _commit(
            self.local_path, message, files=files,
            author_name=author_name, author_email=author_email,
        )

    def _local_head(self) -> str:
        return _git("rev-parse", "HEAD", cwd=self.local_path).stdout.strip()

    def _origin_head(self) -> str:
        return _git("rev-parse", "main", cwd=self.remote_path).stdout.strip()

    def _reconcile(self):
        return reconcile_with_origin(branch="main", cwd=str(self.local_path))


class TestReconcileClean(ReconcileTestBase):
    def test_synced_local_is_noop(self) -> None:
        result = self._reconcile()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.action, "noop")
        self.assertEqual(result.ahead, 0)
        self.assertEqual(result.behind, 0)


class TestReconcileFastForward(ReconcileTestBase):
    def test_x_post_sidecar_advanced_origin_local_fast_forwards(self) -> None:
        """The May 1→2 race: x-post sidecar landed on origin between the
        prior daily and this morning's run. Local should fast-forward
        without spending or committing anything new.
        """
        self._advance_origin(
            "chore(x-post): record sidecar [skip ci]",
            files={"data/x-posts.jsonl": '{"id": "day-N"}\n'},
        )
        before = self._local_head()
        result = self._reconcile()

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.action, "fast-forward")
        self.assertEqual(result.behind, 1)
        self.assertNotEqual(self._local_head(), before)
        self.assertEqual(self._local_head(), self._origin_head())


class TestReconcileRebase(ReconcileTestBase):
    def test_bot_owned_divergence_rebases_and_pushes(self) -> None:
        """The May 2→3 race: x-post sidecar advanced origin AND local
        made a chore(publish): commit on stale HEAD. All local-only
        commits are bot-owned, so reconcile should rebase + push.
        """
        self._advance_origin(
            "chore(x-post): record sidecar [skip ci]",
            files={"data/x-posts.jsonl": '{"id": "day-N"}\n'},
        )
        self._local_commit(
            "chore(publish): 2026-05-02",
            files={"data/briefs.json": "[]\n"},
        )

        result = self._reconcile()

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.action, "rebase")
        self.assertEqual(result.ahead, 1)
        self.assertEqual(result.behind, 1)
        # Local matches origin after rebase + push.
        self.assertEqual(self._local_head(), self._origin_head())
        # Origin's history is linear: initial → sidecar → publish.
        log = _git("log", "--pretty=%s", "main", cwd=self.remote_path).stdout
        self.assertIn("chore(publish): 2026-05-02", log)
        self.assertIn("chore(x-post): record sidecar [skip ci]", log)


class TestReconcileDivergedNonBot(ReconcileTestBase):
    def test_non_bot_local_commit_halts_diverged(self) -> None:
        self._advance_origin(
            "chore(x-post): record sidecar [skip ci]",
            files={"data/x-posts.jsonl": '{"id": "day-N"}\n'},
        )
        self._local_commit(
            "wip: hand edit",
            files={"random.txt": "operator WIP\n"},
            author_name="Some Operator",
            author_email="op@example.com",
        )
        local_after_commit = self._local_head()

        result = self._reconcile()

        self.assertEqual(result.status, "halt")
        self.assertEqual(result.action, "diverged")
        self.assertEqual(result.ahead, 1)
        self.assertEqual(result.behind, 1)
        # Local untouched (no rebase, no abort artifacts).
        self.assertEqual(self._local_head(), local_after_commit)


class TestReconcileBotSubjectWrongAuthor(ReconcileTestBase):
    def test_bot_subject_non_bot_author_still_halts(self) -> None:
        """An operator could accidentally use a chore(publish): subject
        line. The author-name gate must still halt — subject alone is
        not sufficient evidence of bot-ownership.
        """
        self._advance_origin(
            "chore(x-post): record sidecar [skip ci]",
            files={"data/x-posts.jsonl": '{"id": "day-N"}\n'},
        )
        self._local_commit(
            "chore(publish): impersonator",
            files={"data/briefs.json": "[]\n"},
            author_name="Some Operator",
            author_email="op@example.com",
        )

        result = self._reconcile()

        self.assertEqual(result.status, "halt")
        self.assertEqual(result.action, "diverged")


class TestReconcilePushAhead(ReconcileTestBase):
    def test_local_ahead_only_pushes(self) -> None:
        """Existing pre-flight-push behavior preserved: local has an
        unpushed bot commit, origin is unchanged. Reconcile pushes.
        """
        self._local_commit(
            "chore(publish): 2026-05-02",
            files={"data/briefs.json": "[]\n"},
        )

        result = self._reconcile()

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.action, "push")
        self.assertEqual(result.ahead, 1)
        self.assertEqual(self._local_head(), self._origin_head())


class TestReconcileWrongBranch(ReconcileTestBase):
    def test_head_on_other_branch_halts(self) -> None:
        # Defense-in-depth: even though the daily/weekly wrappers have
        # external branch guards, the function itself must refuse to
        # operate when HEAD doesn't match the configured branch.
        # Otherwise push/rebase target the wrong remote ref.
        _git("checkout", "-b", "feature/some-work", cwd=self.local_path)
        before = self._local_head()

        result = self._reconcile()

        self.assertEqual(result.status, "halt")
        self.assertEqual(result.action, "wrong-branch")
        self.assertIn("feature/some-work", result.detail)
        self.assertEqual(self._local_head(), before)

    def test_detached_head_halts(self) -> None:
        head_sha = self._local_head()
        _git("checkout", "--detach", head_sha, cwd=self.local_path)

        result = self._reconcile()

        self.assertEqual(result.status, "halt")
        self.assertEqual(result.action, "wrong-branch")
        self.assertIn("HEAD=HEAD", result.detail)


class TestReconcileDirtyWorktree(ReconcileTestBase):
    def test_modified_tracked_file_halts_before_fetch(self) -> None:
        # Operator left a tracked file modified; the precondition must
        # halt before any fetch/merge/rebase/push that could clobber it.
        (self.local_path / "README.md").write_text("dirty\n")
        before = self._local_head()

        result = self._reconcile()

        self.assertEqual(result.status, "halt")
        self.assertEqual(result.action, "dirty-worktree")
        self.assertEqual(self._local_head(), before)

    def test_staged_change_also_halts(self) -> None:
        (self.local_path / "new.txt").write_text("staged\n")
        _git("add", "new.txt", cwd=self.local_path)
        before = self._local_head()

        result = self._reconcile()

        self.assertEqual(result.status, "halt")
        self.assertEqual(result.action, "dirty-worktree")
        self.assertEqual(self._local_head(), before)

    def test_untracked_only_does_not_halt(self) -> None:
        # Untracked files survive rebase/ff and do not risk operator
        # data loss. They must NOT halt reconciliation; otherwise the
        # PR-development loop (uncommitted new files in the working
        # tree) would falsely jam any local automation run.
        (self.local_path / "scratch.txt").write_text("untracked\n")

        result = self._reconcile()

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.action, "noop")


if __name__ == "__main__":
    unittest.main()
