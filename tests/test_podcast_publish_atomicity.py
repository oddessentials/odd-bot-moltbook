"""Atomic Phase 6 + Phase 7 rollback tests.

Pins the retry-safety contract: if Phase 6 (`cmd_publish`) or Phase 7
(`rebuild_spa_and_verify`) fails after any mutation, the worktree must
return to its pre-cmd_run committed state and the manifest must return
to its pre-publish bytes. Otherwise the wrapper's next-fire
`src.git_sync.reconcile_with_origin` halts on the dirty worktree, the
manifest's `validation_status="published"` misrepresents reality, and
the operator has to clean up by hand.

The user-stated invariant:

    if Phase 6 or Phase 7 fails before the wrapper commit, tracked
    files must return to their pre-run state, and the manifest must
    return to its pre-publish state.

Tests use a sandbox git repo (init + commit minimal fixtures), so
`git restore` + `git clean -fdq -- docs` operate against a real git
worktree without touching the project repo. Subprocess git calls are
contained to `tempfile.mkdtemp()`-rooted dirs.

No Vite invocation, no YouTube/Hedra network, no real `cmd_publish`
side effects — only the rollback mechanics are exercised.

Stdlib unittest + unittest.mock only — run via:

    .venv/bin/python -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.podcast.publish_atomic import (
    _rollback_publish_state,
    atomic_publish_and_verify,
)


def _make_sandbox_repo() -> Path:
    """Init a fresh git repo with the publish-surface files committed.

    Layout mirrors the production repo enough that `git restore`,
    `git clean -fdq -- docs`, and `git status --porcelain` behave the
    way the rollback helper expects.
    """
    td = Path(tempfile.mkdtemp())
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(td)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(td), "config", "user.email", "test@example.invalid"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(td), "config", "user.name", "test"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(td), "config", "commit.gpgsign", "false"],
        check=True, capture_output=True,
    )

    # Mirror the project's `data/*` allowlist for episodes.json.
    (td / ".gitignore").write_text(
        "data/*\n!data/episodes.json\n",
    )
    (td / "data").mkdir()
    (td / "data" / "episodes.json").write_text('[{"id":"ep-001"}]\n')
    docs_assets = td / "docs" / "assets"
    docs_assets.mkdir(parents=True)
    (docs_assets / "index-OLD.js").write_text("// old bundle\n")
    (td / "docs" / "index.html").write_text("<html>v1</html>\n")
    (td / "docs" / "podcast").mkdir()
    (td / "docs" / "podcast" / "ep-001").mkdir()
    (td / "docs" / "podcast" / "ep-001" / "index.html").write_text(
        "<html>ep-001 og</html>\n",
    )
    subprocess.run(
        ["git", "-C", str(td), "add", "."],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(td), "commit", "-q", "-m", "init"],
        check=True, capture_output=True,
    )
    return td


def _porcelain(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout


class TestRollbackPublishState(unittest.TestCase):
    """Direct tests of `_rollback_publish_state` against a sandbox repo."""

    def setUp(self):
        self.repo = _make_sandbox_repo()
        self.addCleanup(shutil.rmtree, self.repo, True)
        # Manifest lives under data/episodes/ep-X/, which is gitignored
        # by the sandbox's `data/*` rule.
        self.manifest = self.repo / "data" / "episodes" / "ep-test" / "manifest.json"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text('{"validation_status":"og_generated"}')

    def test_modified_tracked_episodes_json_restored(self):
        ep = self.repo / "data" / "episodes.json"
        original = ep.read_bytes()
        ep.write_text('[{"id":"ep-MUTATED"}]')

        _rollback_publish_state(
            repo_root=self.repo,
            manifest_path=self.manifest,
            manifest_snap=b'{"validation_status":"og_generated"}',
        )
        self.assertEqual(ep.read_bytes(), original)

    def test_untracked_docs_asset_removed(self):
        # Vite content-hashes asset filenames; a new build produces a
        # new hashed file that's untracked until commit.
        new_asset = self.repo / "docs" / "assets" / "index-NEWHASH.js"
        new_asset.write_text("// new bundle\n")

        _rollback_publish_state(
            repo_root=self.repo,
            manifest_path=self.manifest,
            manifest_snap=b'{"validation_status":"og_generated"}',
        )
        self.assertFalse(
            new_asset.exists(),
            "untracked Vite asset must be removed by git clean",
        )

    def test_deleted_tracked_asset_restored(self):
        # Vite's emptyOutDir unlinks old hashed assets before writing
        # new ones. If the build raises mid-process the deletion sticks.
        old_asset = self.repo / "docs" / "assets" / "index-OLD.js"
        original = old_asset.read_bytes()
        old_asset.unlink()

        _rollback_publish_state(
            repo_root=self.repo,
            manifest_path=self.manifest,
            manifest_snap=b'{"validation_status":"og_generated"}',
        )
        self.assertEqual(old_asset.read_bytes(), original)

    def test_manifest_bytes_restored(self):
        self.manifest.write_text('{"validation_status":"published"}')

        _rollback_publish_state(
            repo_root=self.repo,
            manifest_path=self.manifest,
            manifest_snap=b'{"validation_status":"og_generated"}',
        )
        self.assertEqual(
            self.manifest.read_text(),
            '{"validation_status":"og_generated"}',
        )

    def test_combined_mutation_leaves_porcelain_clean(self):
        # The contract test: simulate every mutation Phase 5/6/7 can
        # produce, then assert `git status --porcelain` is empty.
        (self.repo / "data" / "episodes.json").write_text(
            '[{"id":"ep-MUTATED"}]',
        )
        (self.repo / "docs" / "index.html").write_text("<html>v2</html>\n")
        (self.repo / "docs" / "podcast" / "ep-001" / "index.html").write_text(
            "<html>ep-001 og MUTATED</html>\n",
        )
        # Phase 5 emits an OG page for a new episode → untracked dir.
        (self.repo / "docs" / "podcast" / "ep-test").mkdir()
        (self.repo / "docs" / "podcast" / "ep-test" / "index.html").write_text(
            "<html>ep-test og (would not exist pre-run)</html>\n",
        )
        # Phase 7 rebuild: new hashed asset + deleted old one.
        (self.repo / "docs" / "assets" / "index-NEWHASH.js").write_text(
            "// new build\n",
        )
        (self.repo / "docs" / "assets" / "index-OLD.js").unlink()
        # Manifest already advanced to "published" by Phase 6.
        self.manifest.write_text('{"validation_status":"published"}')

        _rollback_publish_state(
            repo_root=self.repo,
            manifest_path=self.manifest,
            manifest_snap=b'{"validation_status":"og_generated"}',
        )

        self.assertEqual(
            _porcelain(self.repo), "",
            "git status --porcelain must be empty after rollback",
        )
        self.assertEqual(
            self.manifest.read_text(),
            '{"validation_status":"og_generated"}',
        )


class TestAtomicPublishAndVerify(unittest.TestCase):
    """`atomic_publish_and_verify` end-to-end, with cmd_publish + Phase 7
    mocked to mutate the sandbox repo."""

    def setUp(self):
        self.repo = _make_sandbox_repo()
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.manifest = self.repo / "data" / "episodes" / "ep-test" / "manifest.json"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text('{"validation_status":"og_generated"}')
        self.episodes = self.repo / "data" / "episodes.json"

    def test_phase7_failure_rolls_back_all_three_categories(self):
        """User-required regression: cmd_publish mutates episodes.json
        + manifest; rebuild dirties docs/ and raises;
        atomic_publish_and_verify raises; `git status --porcelain` is
        clean; manifest is restored to pre-publish."""

        def fake_publish(args):
            # Phase 6: real-shape mutation.
            self.episodes.write_text(
                '[{"id":"ep-001"},'
                '{"id":"ep-test","youtubeId":"_nNjfWCPUPU"}]'
            )
            self.manifest.write_text('{"validation_status":"published"}')
            return 0

        def fake_rebuild(*, episode_id):
            # Phase 7: dirty docs/ before failing.
            (self.repo / "docs" / "assets" / "index-NEWHASH.js").write_text(
                "// fresh build\n",
            )
            (self.repo / "docs" / "assets" / "index-OLD.js").unlink()
            (self.repo / "docs" / "index.html").write_text("<html>v2</html>\n")
            raise RuntimeError(
                "simulated SPA bundle missing markers: id='ep-test'",
            )

        with mock.patch(
            "src.podcast.publish_atomic.cmd_publish",
            side_effect=fake_publish,
        ), mock.patch(
            "src.podcast.publish_atomic.rebuild_spa_and_verify",
            side_effect=fake_rebuild,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                atomic_publish_and_verify(
                    episode_id="ep-test",
                    manifest_path=self.manifest,
                    repo_root=self.repo,
                )

        # Original failure surfaces.
        self.assertIn("missing markers", str(ctx.exception))

        # Worktree contract.
        self.assertEqual(
            _porcelain(self.repo), "",
            "git status --porcelain must be empty after rollback",
        )

        # Manifest reverted.
        self.assertEqual(
            self.manifest.read_text(),
            '{"validation_status":"og_generated"}',
            "manifest validation_status must return to pre-publish",
        )

    def test_phase7_success_persists_state(self):
        """No rollback on success — mutations are kept."""

        def fake_publish(args):
            self.episodes.write_text(
                '[{"id":"ep-001"},'
                '{"id":"ep-test","youtubeId":"_nNjfWCPUPU"}]'
            )
            self.manifest.write_text('{"validation_status":"published"}')
            return 0

        def fake_rebuild(*, episode_id):
            return None

        with mock.patch(
            "src.podcast.publish_atomic.cmd_publish",
            side_effect=fake_publish,
        ), mock.patch(
            "src.podcast.publish_atomic.rebuild_spa_and_verify",
            side_effect=fake_rebuild,
        ):
            rc = atomic_publish_and_verify(
                episode_id="ep-test",
                manifest_path=self.manifest,
                repo_root=self.repo,
            )

        self.assertEqual(rc, 0)
        self.assertIn("ep-test", self.episodes.read_text())
        self.assertEqual(
            self.manifest.read_text(),
            '{"validation_status":"published"}',
        )

    def test_publish_gate_failure_returns_rc_without_rollback(self):
        """`cmd_publish` returning non-zero means publish_episode raised
        a PublishGateError BEFORE any write. No rollback needed, no
        rebuild attempt."""
        original_episodes = self.episodes.read_bytes()
        original_manifest = self.manifest.read_bytes()
        rebuild = mock.Mock()

        with mock.patch(
            "src.podcast.publish_atomic.cmd_publish",
            return_value=2,
        ), mock.patch(
            "src.podcast.publish_atomic.rebuild_spa_and_verify",
            rebuild,
        ):
            rc = atomic_publish_and_verify(
                episode_id="ep-test",
                manifest_path=self.manifest,
                repo_root=self.repo,
            )

        self.assertEqual(rc, 2)
        rebuild.assert_not_called()
        self.assertEqual(self.episodes.read_bytes(), original_episodes)
        self.assertEqual(self.manifest.read_bytes(), original_manifest)

    def test_retry_after_phase7_failure_proceeds_cleanly(self):
        """The retry contract: after a rolled-back failure, a fresh
        attempt with the same mocked publisher must succeed and leave
        coherent state."""

        def fake_publish(args):
            self.episodes.write_text(
                '[{"id":"ep-001"},'
                '{"id":"ep-test","youtubeId":"_nNjfWCPUPU"}]'
            )
            self.manifest.write_text('{"validation_status":"published"}')
            return 0

        # First attempt: Phase 7 fails.
        with mock.patch(
            "src.podcast.publish_atomic.cmd_publish",
            side_effect=fake_publish,
        ), mock.patch(
            "src.podcast.publish_atomic.rebuild_spa_and_verify",
            side_effect=RuntimeError("first attempt failure"),
        ):
            with self.assertRaises(RuntimeError):
                atomic_publish_and_verify(
                    episode_id="ep-test",
                    manifest_path=self.manifest,
                    repo_root=self.repo,
                )
        self.assertEqual(
            _porcelain(self.repo), "",
            "worktree must be clean after first-attempt rollback",
        )

        # Second attempt: same inputs, Phase 7 succeeds.
        with mock.patch(
            "src.podcast.publish_atomic.cmd_publish",
            side_effect=fake_publish,
        ), mock.patch(
            "src.podcast.publish_atomic.rebuild_spa_and_verify",
            return_value=None,
        ):
            rc = atomic_publish_and_verify(
                episode_id="ep-test",
                manifest_path=self.manifest,
                repo_root=self.repo,
            )
        self.assertEqual(rc, 0)
        self.assertIn("ep-test", self.episodes.read_text())
        self.assertEqual(
            self.manifest.read_text(),
            '{"validation_status":"published"}',
        )


if __name__ == "__main__":
    unittest.main()
