"""Unit test for src.podcast.stitch.stitch_episode output-path derivation.

The ffmpeg invocation itself is integration territory (real binary, real
inputs). What's pinned here is the trust boundary: the final.mp4 +
concat.txt output paths come from `manifest_path.parent`, never from
`manifest["id"]`. A tampered manifest claiming `id: "../malicious"` must
not direct ffmpeg to write outside the operator's episode directory.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.podcast import stitch as stitch_module
from src.podcast.manifest import write_manifest
from src.podcast.stitch import stitch_episode


class TestStitchEpisodeWritePath(unittest.TestCase):
    def test_final_path_anchored_on_manifest_parent_not_manifest_id(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            ep_dir = tdp / "data" / "episodes" / "ep-test"
            clips_subdir = ep_dir / "clips"
            clips_subdir.mkdir(parents=True)
            for i in range(2):
                (clips_subdir / f"seg{i:02d}.mp4").write_bytes(b"clip")

            mpath = ep_dir / "manifest.json"
            write_manifest(
                mpath,
                {
                    "id": "../malicious",
                    "segments": [
                        {
                            "clip_status": "complete",
                            "clip_path": f"data/episodes/ep-test/clips/seg{i:02d}.mp4",
                        }
                        for i in range(2)
                    ],
                },
            )

            captured: list[list[str]] = []

            class FakeProc:
                returncode = 0
                stderr = ""
                stdout = ""

            def fake_run(cmd, *args, **kwargs):
                captured.append(cmd)
                return FakeProc()

            from src.podcast import manifest as manifest_module

            with mock.patch.object(manifest_module, "REPO_ROOT", tdp), \
                 mock.patch.object(subprocess, "run", side_effect=fake_run):
                # Make a placeholder so the FileExistsError branch doesn't
                # block the call. Actually, final.mp4 doesn't exist yet —
                # stitch_episode will create it via ffmpeg. The fake_run
                # doesn't actually invoke ffmpeg, so we plant the file
                # ourselves AFTER the call to satisfy any read-back.
                final_path = stitch_episode(manifest_path=mpath)

            self.assertEqual(final_path, ep_dir / "final.mp4")

            self.assertEqual(len(captured), 1, "ffmpeg should be invoked exactly once")
            cmd = captured[0]
            # The last argument to ffmpeg is the output path. Anchor it.
            self.assertEqual(cmd[-1], str(ep_dir / "final.mp4"))
            # The concat list file path appears in the args after `-i`.
            i_flag_idx = cmd.index("-i")
            list_arg = cmd[i_flag_idx + 1]
            self.assertEqual(list_arg, str(ep_dir / "concat.txt"))


if __name__ == "__main__":
    unittest.main()
