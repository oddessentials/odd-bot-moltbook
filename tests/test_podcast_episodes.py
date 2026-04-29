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

    def test_write_sorts_newest_first_by_episode_no(self):
        # Order is load-bearing for the SPA — Home.tsx + Podcast.tsx
        # both treat episodes[0] as the latest. Engine writes episodeNo
        # descending so episodes[0] = newest.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "episodes.json"
            r1 = EpisodeRecord(**_record_payload(id="ep-001", episodeNo=1))
            r2 = EpisodeRecord(**_record_payload(id="ep-002", episodeNo=2))
            r3 = EpisodeRecord(**_record_payload(id="ep-003", episodeNo=3))
            _write_episodes_json([r1, r3, r2], path)
            payload = json.loads(path.read_text())
            self.assertEqual([e["id"] for e in payload], ["ep-003", "ep-002", "ep-001"])

    def test_write_id_descending_tiebreaks_when_episode_no_collides(self):
        # Defensive: if a future operator pre-publishes a re-issue with
        # the same episodeNo (or some test fixture creates a tie), the
        # secondary sort key (id, descending) deterministically orders.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "episodes.json"
            r1 = EpisodeRecord(**_record_payload(id="ep-001", episodeNo=1))
            r2 = EpisodeRecord(**_record_payload(id="ep-001b", episodeNo=1))
            _write_episodes_json([r1, r2], path)
            payload = json.loads(path.read_text())
            self.assertEqual([e["id"] for e in payload], ["ep-001b", "ep-001"])


class TestCmdFlipPublic(unittest.TestCase):
    """Phase 2.4 manual flip gate. videos.update is mocked; what's pinned
    is the gate (refuses below 'published') and the verify step that
    confirms the API actually moved privacyStatus to 'public'."""

    EPISODE_ID = "ep-test"

    def _seed(self, tdp: Path, *, validation_status: str, youtube_id=None) -> Path:
        ep_dir = tdp / "data" / "episodes" / self.EPISODE_ID
        ep_dir.mkdir(parents=True)
        manifest = {
            "id": self.EPISODE_ID,
            "validation_status": validation_status,
        }
        if youtube_id is not None:
            manifest["youtube_id"] = youtube_id
        path = ep_dir / "manifest.json"
        write_manifest(path, manifest)
        return path

    def _patch_dirs(self, tdp: Path):
        return mock.patch.multiple(
            manifest_module,
            REPO_ROOT=tdp,
            EPISODES_DIR=tdp / "data" / "episodes",
        )

    def test_refuses_if_below_published(self):
        from src.podcast.episodes import cmd_flip_public
        import argparse
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._seed(tdp, validation_status="uploaded", youtube_id="abc")
            with self._patch_dirs(tdp), \
                 mock.patch("src.podcast.keys.load_youtube_credentials") as mck_creds, \
                 mock.patch("src.podcast.youtube.set_youtube_privacy") as mck_set, \
                 mock.patch("src.podcast.youtube.verify_youtube_video") as mck_verify:
                rc = cmd_flip_public(argparse.Namespace(episode_id=self.EPISODE_ID))
            self.assertEqual(rc, 2)
            mck_creds.assert_not_called()
            mck_set.assert_not_called()
            mck_verify.assert_not_called()

    def test_refuses_at_og_generated(self):
        from src.podcast.episodes import cmd_flip_public
        import argparse
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._seed(tdp, validation_status="og_generated", youtube_id="abc")
            with self._patch_dirs(tdp), \
                 mock.patch("src.podcast.youtube.set_youtube_privacy") as mck_set, \
                 mock.patch("src.podcast.youtube.verify_youtube_video"):
                rc = cmd_flip_public(argparse.Namespace(episode_id=self.EPISODE_ID))
            self.assertEqual(rc, 2)
            mck_set.assert_not_called()

    def test_refuses_if_youtube_id_missing(self):
        from src.podcast.episodes import cmd_flip_public
        import argparse
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._seed(tdp, validation_status="published", youtube_id=None)
            with self._patch_dirs(tdp), \
                 mock.patch("src.podcast.youtube.set_youtube_privacy") as mck_set:
                rc = cmd_flip_public(argparse.Namespace(episode_id=self.EPISODE_ID))
            self.assertEqual(rc, 2)
            mck_set.assert_not_called()

    def test_happy_path_flips_and_advances_to_live(self):
        from src.podcast.episodes import cmd_flip_public
        import argparse
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath = self._seed(tdp, validation_status="published", youtube_id="abc123")

            def fake_verify(*, credentials, video_id):
                return {"id": video_id, "status": {"privacyStatus": "public"}}

            with self._patch_dirs(tdp), \
                 mock.patch("src.podcast.keys.load_youtube_credentials", return_value=object()), \
                 mock.patch("src.podcast.youtube.set_youtube_privacy") as mck_set, \
                 mock.patch("src.podcast.youtube.verify_youtube_video", side_effect=fake_verify):
                rc = cmd_flip_public(argparse.Namespace(episode_id=self.EPISODE_ID))

            self.assertEqual(rc, 0)
            mck_set.assert_called_once()
            kwargs = mck_set.call_args.kwargs
            self.assertEqual(kwargs["video_id"], "abc123")
            self.assertEqual(kwargs["privacy_status"], "public")
            self.assertEqual(json.loads(mpath.read_text())["validation_status"], "live")

    def test_raises_if_verify_shows_non_public(self):
        # Even with a successful update API call, re-confirm via
        # videos.list. If YouTube still reports "unlisted", manifest
        # must NOT advance to "live".
        from src.podcast.episodes import cmd_flip_public
        import argparse
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath = self._seed(tdp, validation_status="published", youtube_id="abc123")

            def fake_verify(*, credentials, video_id):
                return {"id": video_id, "status": {"privacyStatus": "unlisted"}}

            with self._patch_dirs(tdp), \
                 mock.patch("src.podcast.keys.load_youtube_credentials", return_value=object()), \
                 mock.patch("src.podcast.youtube.set_youtube_privacy"), \
                 mock.patch("src.podcast.youtube.verify_youtube_video", side_effect=fake_verify), \
                 self.assertRaises(RuntimeError) as cm:
                cmd_flip_public(argparse.Namespace(episode_id=self.EPISODE_ID))
            self.assertIn("unlisted", str(cm.exception))
            self.assertEqual(json.loads(mpath.read_text())["validation_status"], "published")


if __name__ == "__main__":
    unittest.main()
