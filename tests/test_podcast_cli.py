"""Unit test for src.podcast.cli.cmd_upload's consume-side trust check.

A tampered manifest["stitched_path"] must be refused — cmd_upload must
not read the escape path with ffprobe and must not push it to YouTube.
The check fires before any network / filesystem side effects, so the
test simply asserts an early exit and that the downstream helpers were
not called.
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.podcast import cli as cli_module
from src.podcast import manifest as manifest_module
from src.podcast.manifest import write_manifest


class TestCmdUploadStitchedPathSandbox(unittest.TestCase):
    EPISODE_ID = "ep-test"

    def _seed(self, tdp: Path, stitched_path_value: str) -> Path:
        ep_dir = tdp / "data" / "episodes" / self.EPISODE_ID
        ep_dir.mkdir(parents=True)
        path = ep_dir / "manifest.json"
        write_manifest(
            path,
            {
                "id": self.EPISODE_ID,
                "stitched_path": stitched_path_value,
                "validation_status": "stitched",
                "segments": [],
                "script": {"title": "T", "description": "D" * 200, "segments": []},
            },
        )
        return path

    def _run_with_tampered_stitched_path(self, *, stitched_path: str, plant_at: Path | None = None) -> int:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath = self._seed(tdp, stitched_path)

            # Plant a real file at the escape destination to prove the
            # refusal isn't merely "file not found." If plant_at is None,
            # the test relies purely on the boundary check (which fires
            # before any existence probe).
            if plant_at is not None:
                plant_at.parent.mkdir(parents=True, exist_ok=True)
                plant_at.write_bytes(b"attacker-mp4")

            generate_srt_called = False
            load_creds_called = False
            ffprobe_called = False

            def fake_generate_srt(*a, **kw):
                nonlocal generate_srt_called
                generate_srt_called = True
                raise AssertionError("generate_srt must not be called on a refused upload")

            def fake_load_creds(*a, **kw):
                nonlocal load_creds_called
                load_creds_called = True
                raise AssertionError("load_youtube_credentials must not be called on a refused upload")

            def fake_ffprobe(*a, **kw):
                nonlocal ffprobe_called
                ffprobe_called = True
                raise AssertionError("ffprobe must not run against a refused stitched_path")

            with mock.patch.object(manifest_module, "REPO_ROOT", tdp), \
                 mock.patch.object(manifest_module, "EPISODES_DIR", tdp / "data" / "episodes"), \
                 mock.patch.object(cli_module, "REPO_ROOT", tdp), \
                 mock.patch.object(cli_module, "generate_srt", side_effect=fake_generate_srt), \
                 mock.patch.object(cli_module, "load_youtube_credentials", side_effect=fake_load_creds), \
                 mock.patch.object(cli_module, "ffprobe_streams", side_effect=fake_ffprobe):
                rc = cli_module.cmd_upload(
                    argparse.Namespace(episode_id=self.EPISODE_ID)
                )

            self.assertFalse(generate_srt_called)
            self.assertFalse(load_creds_called)
            self.assertFalse(ffprobe_called)
            return rc

    def test_relative_dotdot_escape_refused(self):
        rc = self._run_with_tampered_stitched_path(
            stitched_path="../malicious.mp4",
        )
        self.assertEqual(rc, 2)

    def test_absolute_path_refused(self):
        with tempfile.TemporaryDirectory() as plant_td:
            attacker_file = Path(plant_td) / "attacker.mp4"
            attacker_file.write_bytes(b"attacker-mp4")
            rc = self._run_with_tampered_stitched_path(
                stitched_path=str(attacker_file),
                plant_at=attacker_file,
            )
            self.assertEqual(rc, 2)

    def test_deep_dotdot_escape_with_real_file_at_target_refused(self):
        # Plant a real .mp4 at the escape destination so the refusal isn't
        # just "file doesn't exist" — confirms the boundary check fires
        # before any existence probe.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath = self._seed(tdp, "data/episodes/ep-test/../../../outside.mp4")
            (tdp / "outside.mp4").write_bytes(b"attacker")

            with mock.patch.object(manifest_module, "REPO_ROOT", tdp), \
                 mock.patch.object(manifest_module, "EPISODES_DIR", tdp / "data" / "episodes"), \
                 mock.patch.object(cli_module, "REPO_ROOT", tdp), \
                 mock.patch.object(cli_module, "generate_srt", side_effect=AssertionError("must not call")), \
                 mock.patch.object(cli_module, "load_youtube_credentials", side_effect=AssertionError("must not call")), \
                 mock.patch.object(cli_module, "ffprobe_streams", side_effect=AssertionError("must not call")):
                rc = cli_module.cmd_upload(
                    argparse.Namespace(episode_id=self.EPISODE_ID)
                )
            self.assertEqual(rc, 2)


class _RunHarness(unittest.TestCase):
    """Shared seed/mocks for cmd_run + cmd_resume_publish resumability tests."""

    EPISODE_ID = "ep-test"

    def _seed(self, tdp: Path, *, validation_status: str, youtube_id: str | None) -> Path:
        ep_dir = tdp / "data" / "episodes" / self.EPISODE_ID
        ep_dir.mkdir(parents=True)
        mpath = ep_dir / "manifest.json"
        payload = {
            "id": self.EPISODE_ID,
            "validation_status": validation_status,
            "segments": [],
            "script": {"title": "T", "description": "D" * 200, "segments": []},
        }
        if youtube_id is not None:
            payload["youtube_id"] = youtube_id
        write_manifest(mpath, payload)
        return mpath

    def _counting_mocks(self, tdp: Path, calls: dict):
        def rec(name):
            def _f(*a, **k):
                calls[name] += 1
                return 0
            return _f
        return (
            mock.patch.object(manifest_module, "REPO_ROOT", tdp),
            mock.patch.object(manifest_module, "EPISODES_DIR", tdp / "data" / "episodes"),
            mock.patch.object(cli_module, "cmd_produce_segments", side_effect=rec("produce")),
            mock.patch.object(cli_module, "cmd_stitch", side_effect=rec("stitch")),
            mock.patch.object(cli_module, "cmd_upload", side_effect=rec("upload")),
            mock.patch.object(cli_module, "cmd_og", side_effect=rec("og")),
            mock.patch(
                "src.podcast.publish_atomic.atomic_publish_and_verify",
                side_effect=rec("publish"),
            ),
        )


class TestCmdRunFastPath(_RunHarness):
    def _run(self, tdp: Path):
        calls = {k: 0 for k in ("produce", "stitch", "upload", "og", "publish")}
        patches = self._counting_mocks(tdp, calls)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            rc = cli_module.cmd_run(
                argparse.Namespace(
                    episode_id=self.EPISODE_ID, episode_no=None, run_date=None,
                    parallel=4, max_attempts=None, force=False,
                )
            )
        return rc, calls

    def test_uploaded_episode_skips_production_and_publishes(self):
        # The resumability invariant: a video already on YouTube posts on
        # re-run WITHOUT re-invoking produce/stitch/upload (no ElevenLabs/Hedra).
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._seed(tdp, validation_status="uploaded", youtube_id="vid123")
            rc, calls = self._run(tdp)
            self.assertEqual(rc, 0)
            self.assertEqual(calls["produce"], 0)
            self.assertEqual(calls["stitch"], 0)
            self.assertEqual(calls["upload"], 0)
            self.assertEqual(calls["og"], 1)
            self.assertEqual(calls["publish"], 1)

    def test_not_yet_uploaded_episode_runs_production(self):
        # No youtube_id → must NOT fast-path; the create phases still run.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._seed(tdp, validation_status="stitched", youtube_id=None)
            rc, calls = self._run(tdp)
            self.assertEqual(rc, 0)
            self.assertEqual(calls["produce"], 1)
            self.assertEqual(calls["stitch"], 1)
            self.assertEqual(calls["upload"], 1)
            self.assertEqual(calls["publish"], 1)

    def test_youtube_id_without_upload_status_does_not_fast_path(self):
        # Defense in depth: a youtube_id on a manifest whose status hasn't
        # reached the gate (tampered/partial) must not skip production.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._seed(tdp, validation_status="stitched", youtube_id="vid123")
            rc, calls = self._run(tdp)
            self.assertEqual(rc, 0)
            self.assertEqual(calls["produce"], 1)

    def test_video_uploaded_boundary_does_not_fast_path(self):
        # The gate is >= "uploaded", NOT >= "video_uploaded": at exactly
        # video_uploaded the manifest has no episode_record/caption_id yet
        # (cmd_upload writes those on reaching "uploaded"), so the publish tail
        # would fail. Such an episode must fall through to the create phases —
        # cmd_upload finishes the caption/record — not fast-path.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._seed(tdp, validation_status="video_uploaded", youtube_id="vid123")
            rc, calls = self._run(tdp)
            self.assertEqual(rc, 0)
            self.assertEqual(calls["produce"], 1)
            self.assertEqual(calls["upload"], 1)


class TestCmdResumePublish(_RunHarness):
    def _run(self, tdp: Path):
        calls = {k: 0 for k in ("produce", "stitch", "upload", "og", "publish")}
        patches = self._counting_mocks(tdp, calls)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            rc = cli_module.cmd_resume_publish(
                argparse.Namespace(episode_id=self.EPISODE_ID)
            )
        return rc, calls

    def test_posts_when_uploaded(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._seed(tdp, validation_status="uploaded", youtube_id="vid123")
            rc, calls = self._run(tdp)
            self.assertEqual(rc, 0)
            self.assertEqual(calls["og"], 1)
            self.assertEqual(calls["publish"], 1)
            self.assertEqual(calls["produce"], 0)

    def test_refuses_at_video_uploaded_boundary(self):
        # video_uploaded is not a resumable-publish state (episode_record and
        # caption_id are not written until "uploaded"); refuse rather than
        # fail mid-tail.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._seed(tdp, validation_status="video_uploaded", youtube_id="vid123")
            rc, calls = self._run(tdp)
            self.assertEqual(rc, 2)
            self.assertEqual(calls["og"], 0)
            self.assertEqual(calls["publish"], 0)

    def test_refuses_without_youtube_id(self):
        # No uploaded video → must refuse and never render OG or publish.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._seed(tdp, validation_status="stitched", youtube_id=None)
            rc, calls = self._run(tdp)
            self.assertEqual(rc, 2)
            self.assertEqual(calls["og"], 0)
            self.assertEqual(calls["publish"], 0)

    def test_refuses_when_manifest_missing(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "data" / "episodes").mkdir(parents=True)
            rc, calls = self._run(tdp)
            self.assertEqual(rc, 2)
            self.assertEqual(calls["og"], 0)
            self.assertEqual(calls["publish"], 0)


if __name__ == "__main__":
    unittest.main()
