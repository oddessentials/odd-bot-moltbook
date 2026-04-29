"""Unit tests for src.podcast.episodes — the data/episodes.json publish-event
writer. Each hard gate (G1..G5) gets a refusal test; the happy path
proves the write content + dedup semantics; an integration-shaped test
proves a partial-success state never advances the public surface.

ffprobe / videos.list / network paths are mocked so the gates are
exercised in isolation against the manifest's recorded state.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.podcast import episodes as episodes_module
from src.podcast import manifest as manifest_module
from src.podcast.episodes import (
    PublishGateError,
    _read_episodes_json,
    _write_episodes_json,
    publish_episode,
)
from src.podcast.manifest import write_manifest
from src.podcast.schema import EpisodeRecord


# --- Fixtures ------------------------------------------------------------

EPISODE_ID = "ep-test"


def _record_payload(**overrides) -> dict:
    payload = {
        "id": EPISODE_ID,
        "episodeNo": 1,
        "title": "Title",
        "date": "2026-04-28",
        "durationMinutes": 4,
        "youtubeId": "abc123",
        "description": "An episode description that is long enough.",
        "hosts": ["Shrimp"],
    }
    payload.update(overrides)
    return payload


def _full_manifest(**overrides) -> dict:
    manifest = {
        "id": EPISODE_ID,
        "youtube_id": "abc123",
        "youtube_caption_id": "caption_xyz",
        "stitched_path": f"data/episodes/{EPISODE_ID}/final.mp4",
        "og_html_path": f"docs/podcast/{EPISODE_ID}/index.html",
        "episode_record": _record_payload(),
        "validation_status": "uploaded",
        "segments": [],
        "script": {"title": "Title", "description": "D" * 200, "segments": []},
    }
    manifest.update(overrides)
    return manifest


def _seed(td: Path, **manifest_overrides) -> tuple[Path, Path, Path]:
    """Lay out a fake repo under `td` with manifest, final.mp4, OG page.
    Returns (manifest_path, final_path, og_path)."""
    ep_dir = td / "data" / "episodes" / EPISODE_ID
    ep_dir.mkdir(parents=True)
    final_path = ep_dir / "final.mp4"
    final_path.write_bytes(b"mp4")

    og_dir = td / "docs" / "podcast" / EPISODE_ID
    og_dir.mkdir(parents=True)
    og_path = og_dir / "index.html"
    og_path.write_bytes(b"<html></html>")

    manifest = _full_manifest(**manifest_overrides)
    mpath = ep_dir / "manifest.json"
    write_manifest(mpath, manifest)
    return mpath, final_path, og_path


def _patch_dirs(tdp: Path):
    return mock.patch.multiple(
        manifest_module,
        REPO_ROOT=tdp,
        EPISODES_DIR=tdp / "data" / "episodes",
    ), mock.patch.multiple(
        episodes_module,
        EPISODES_PUBLIC_PATH=tdp / "data" / "episodes.json",
        PODCAST_OG_DIR=tdp / "docs" / "podcast",
    )


def _fake_verify_ok(*, credentials, video_id):
    return {"id": video_id, "snippet": {}, "status": {"privacyStatus": "unlisted"}}


def _fake_ffprobe_ok(_path):
    return {"format": {"duration": "240.0"}, "streams": []}


def _run_publish(tdp: Path, mpath: Path, *, episodes_path: Path | None = None,
                 fake_verify=_fake_verify_ok, fake_ffprobe=_fake_ffprobe_ok):
    repo_patch, episodes_patch = _patch_dirs(tdp)
    epath = episodes_path or (tdp / "data" / "episodes.json")
    with repo_patch, episodes_patch, \
         mock.patch("src.podcast.youtube.verify_youtube_video", side_effect=fake_verify), \
         mock.patch.object(episodes_module, "ffprobe_streams", side_effect=fake_ffprobe):
        return publish_episode(
            manifest_path=mpath,
            credentials=object(),
            episodes_path=epath,
        )


# --- Happy path ----------------------------------------------------------

class TestPublishEpisodeHappyPath(unittest.TestCase):
    def test_writes_episode_to_episodes_json(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath, _, _ = _seed(tdp)
            epath = tdp / "data" / "episodes.json"

            record = _run_publish(tdp, mpath, episodes_path=epath)

            self.assertEqual(record.id, EPISODE_ID)
            self.assertTrue(epath.exists())
            payload = json.loads(epath.read_text())
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["id"], EPISODE_ID)
            self.assertEqual(payload[0]["youtubeId"], "abc123")

    def test_dedup_replaces_existing_entry_by_id(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath, _, _ = _seed(tdp)
            epath = tdp / "data" / "episodes.json"
            # Pre-existing entry with the same id but stale fields. The
            # publish must replace, not append.
            stale = _record_payload(title="Stale title", episodeNo=99)
            epath.parent.mkdir(parents=True, exist_ok=True)
            epath.write_text(json.dumps([stale]) + "\n")

            _run_publish(tdp, mpath, episodes_path=epath)

            payload = json.loads(epath.read_text())
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["title"], "Title")  # fresh
            self.assertEqual(payload[0]["episodeNo"], 1)

    def test_appends_when_id_does_not_match_existing(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath, _, _ = _seed(tdp)
            epath = tdp / "data" / "episodes.json"
            other = _record_payload(id="ep-002", episodeNo=2)
            epath.parent.mkdir(parents=True, exist_ok=True)
            epath.write_text(json.dumps([other]) + "\n")

            _run_publish(tdp, mpath, episodes_path=epath)

            payload = json.loads(epath.read_text())
            ids = [e["id"] for e in payload]
            self.assertEqual(sorted(ids), ["ep-002", EPISODE_ID])


# --- Gate refusals -------------------------------------------------------

class TestPublishEpisodeGates(unittest.TestCase):
    def _expect_no_write_and_gate(self, *, tdp: Path, mpath: Path, gate_token: str,
                                  fake_verify=_fake_verify_ok, fake_ffprobe=_fake_ffprobe_ok):
        epath = tdp / "data" / "episodes.json"
        with self.assertRaises(PublishGateError) as cm:
            _run_publish(tdp, mpath, episodes_path=epath,
                         fake_verify=fake_verify, fake_ffprobe=fake_ffprobe)
        self.assertIn(gate_token, str(cm.exception))
        # Critical contract: NO write on partial success.
        self.assertFalse(epath.exists())

    def test_g1_missing_youtube_id(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath, _, _ = _seed(tdp, youtube_id=None)
            self._expect_no_write_and_gate(tdp=tdp, mpath=mpath, gate_token="G1")

    def test_g1_videos_list_returns_wrong_id(self):
        def fake(credentials, video_id):
            return {"id": "different-id", "status": {"privacyStatus": "unlisted"}}
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath, _, _ = _seed(tdp)

            self._expect_no_write_and_gate(
                tdp=tdp, mpath=mpath, gate_token="G1",
                fake_verify=lambda *, credentials, video_id: fake(credentials, video_id),
            )

    def test_g2_missing_episode_record(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath, _, _ = _seed(tdp, episode_record=None)
            self._expect_no_write_and_gate(tdp=tdp, mpath=mpath, gate_token="G2")

    def test_g2_malformed_episode_record(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            bad = _record_payload(date="not-a-date")
            mpath, _, _ = _seed(tdp, episode_record=bad)
            self._expect_no_write_and_gate(tdp=tdp, mpath=mpath, gate_token="G2")

    def test_g3_missing_stitched_path(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath, _, _ = _seed(tdp, stitched_path=None)
            self._expect_no_write_and_gate(tdp=tdp, mpath=mpath, gate_token="G3")

    def test_g3_stitched_path_escape(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath, _, _ = _seed(tdp, stitched_path="../escape.mp4")
            self._expect_no_write_and_gate(tdp=tdp, mpath=mpath, gate_token="G3")

    def test_g3_final_mp4_missing(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath, final_path, _ = _seed(tdp)
            final_path.unlink()
            self._expect_no_write_and_gate(tdp=tdp, mpath=mpath, gate_token="G3")

    def test_g3_duration_too_short(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath, _, _ = _seed(tdp)
            self._expect_no_write_and_gate(
                tdp=tdp, mpath=mpath, gate_token="G3",
                fake_ffprobe=lambda _p: {"format": {"duration": "5.0"}, "streams": []},
            )

    def test_g3_duration_too_long(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath, _, _ = _seed(tdp)
            self._expect_no_write_and_gate(
                tdp=tdp, mpath=mpath, gate_token="G3",
                fake_ffprobe=lambda _p: {"format": {"duration": "9999.0"}, "streams": []},
            )

    def test_g4_missing_caption(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath, _, _ = _seed(tdp, youtube_caption_id=None)
            self._expect_no_write_and_gate(tdp=tdp, mpath=mpath, gate_token="G4")

    def test_g5_missing_og_html(self):
        # The fail-closed default: until Phase 2.2's OG generator lands
        # and populates og_html_path, every publish refuses.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath, _, _ = _seed(tdp, og_html_path=None)
            self._expect_no_write_and_gate(tdp=tdp, mpath=mpath, gate_token="G5")

    def test_g5_og_html_escape(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath, _, _ = _seed(tdp, og_html_path="../escape.html")
            self._expect_no_write_and_gate(tdp=tdp, mpath=mpath, gate_token="G5")

    def test_g5_og_html_file_missing(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath, _, og_path = _seed(tdp)
            og_path.unlink()
            self._expect_no_write_and_gate(tdp=tdp, mpath=mpath, gate_token="G5")


# --- Read/write helpers --------------------------------------------------

class TestEpisodesJsonReadWrite(unittest.TestCase):
    def test_read_returns_empty_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(_read_episodes_json(Path(td) / "missing.json"), [])

    def test_read_rejects_malformed_json(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "episodes.json"
            path.write_text("{not valid json")
            with self.assertRaises(PublishGateError):
                _read_episodes_json(path)

    def test_read_rejects_invalid_episode_shape(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "episodes.json"
            path.write_text(json.dumps([{"id": "x"}]))  # missing required fields
            with self.assertRaises(PublishGateError):
                _read_episodes_json(path)

    def test_write_sorts_ascending_by_id(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "episodes.json"
            r1 = EpisodeRecord(**_record_payload(id="ep-002", episodeNo=2))
            r2 = EpisodeRecord(**_record_payload(id="ep-001", episodeNo=1))
            _write_episodes_json([r1, r2], path)
            payload = json.loads(path.read_text())
            self.assertEqual([e["id"] for e in payload], ["ep-001", "ep-002"])


if __name__ == "__main__":
    unittest.main()
