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


if __name__ == "__main__":
    unittest.main()
