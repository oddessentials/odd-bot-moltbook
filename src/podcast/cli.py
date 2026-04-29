"""argparse + cmd_* dispatch for `python -m src.podcast`.

This is the seam where flags become engine calls. The cmd_* functions are
thin: load inputs, call into the right module, write outputs to the
manifest, print the operator-facing line. Heavy logic lives in the
specialised modules.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as _date
from pathlib import Path

from .cast import load_cast
from .config import (
    DEFAULT_VISIBILITY,
    REPO_ROOT,
    SCRIPT_MODEL,
    YOUTUBE_DEFAULT_TAGS,
    YOUTUBE_DISCLAIMER,
)
from .corpus import load_eligible_corpus, summarize_corpus
from .hedra import hedra_session
from .keys import (
    load_elevenlabs_key,
    load_hedra_key,
    load_youtube_credentials,
)
from .manifest import (
    derive_episode_no,
    derive_hosts,
    episode_dir,  # noqa: F401  -- used by cmd_generate_script via --force cleanup
    manifest_path_for,
    read_manifest,
    write_initial_manifest,
    write_manifest,
)
from .media import ffprobe_streams, generate_srt
from .schema import EpisodeRecord, EpisodeScript
from .scripting import generate_episode_script
from .segments import process_segment
from .stitch import stitch_episode, validate_stitched_output
from .youtube import (
    resume_youtube_upload,
    upload_youtube_caption,
    upload_youtube_video,
    verify_youtube_video,
)


def cmd_show_corpus(args: argparse.Namespace) -> int:
    corpus = load_eligible_corpus()
    print(f"Episode 1 eligible corpus: {len(corpus)} brief(s)")
    print(summarize_corpus(corpus))
    return 0


def cmd_generate_script(args: argparse.Namespace) -> int:
    corpus = load_eligible_corpus()
    if not corpus:
        print("no eligible corpus — refusing to generate", file=sys.stderr)
        return 2

    episode_id = args.episode_id
    mpath = manifest_path_for(episode_id)
    if mpath.exists() and not args.force:
        print(
            f"manifest already exists at {mpath}. "
            "Pass --force to overwrite (drops all per-segment pipeline state "
            "AND wipes any stale audio/clips/final.mp4/captions on disk).",
            file=sys.stderr,
        )
        return 2

    if args.force:
        edir = episode_dir(episode_id)
        if edir.exists():
            shutil.rmtree(edir)
            print(f"Wiped stale {edir} (--force)")

    cast = load_cast()
    print(f"Generating script (model={SCRIPT_MODEL}) over {len(corpus)} brief(s)...")
    script = generate_episode_script(corpus, cast)

    episode_no = args.episode_no or derive_episode_no()
    run_date = args.run_date or _date.today().isoformat()
    manifest_path = write_initial_manifest(
        episode_id=episode_id,
        episode_no=episode_no,
        run_date=run_date,
        corpus=corpus,
        cast=cast,
        script=script,
        overwrite=args.force,
    )
    print(f"Script generated: {len(script.segments)} segments, title={script.title!r}")
    print(f"Hosts: {derive_hosts(script, cast)}")
    print(f"Manifest: {manifest_path}")
    return 0


def cmd_produce_segments(args: argparse.Namespace) -> int:
    """Canary-then-scale TTS + Hedra clip generation across all manifest segments.

    Segment 0 runs as the canary. If its objective gates pass, segments
    1..N-1 run with the same gates. Any segment failure aborts the run
    with the manifest left in whatever partial state it reached — the
    next invocation can reuse already-complete segments.
    """
    eid = args.episode_id
    mpath = manifest_path_for(eid)
    if not mpath.exists():
        print(f"manifest missing at {mpath} — run generate-script first.", file=sys.stderr)
        return 2

    manifest = read_manifest(mpath)
    segments = manifest["segments"]
    n = len(segments)
    print(f"Producing {n} segment(s) for episode {eid}")

    cast = load_cast()
    e_key = load_elevenlabs_key()
    h_session = hedra_session(load_hedra_key())

    print("Canary: segment 0...")
    process_segment(
        manifest_path=mpath, idx=0, cast=cast,
        elevenlabs_key=e_key, hedra_session=h_session,
    )
    print("Canary green. Scaling to remaining segments in parallel...")

    parallel_workers = min(args.parallel, max(1, n - 1))
    if parallel_workers <= 1 or n <= 2:
        for idx in range(1, n):
            process_segment(
                manifest_path=mpath, idx=idx, cast=cast,
                elevenlabs_key=e_key, hedra_session=h_session,
            )
    else:
        with ThreadPoolExecutor(max_workers=parallel_workers) as ex:
            futures = {
                ex.submit(
                    process_segment,
                    manifest_path=mpath, idx=idx, cast=cast,
                    elevenlabs_key=e_key, hedra_session=h_session,
                ): idx
                for idx in range(1, n)
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    print(f"  seg{idx:02d}: FAILED — {e}", file=sys.stderr)
                    raise

    manifest = read_manifest(mpath)
    manifest["validation_status"] = "segments_complete"
    write_manifest(mpath, manifest)
    print(f"All {n} segments complete. validation_status=segments_complete")
    return 0


def cmd_stitch(args: argparse.Namespace) -> int:
    eid = args.episode_id
    mpath = manifest_path_for(eid)
    if not mpath.exists():
        print(f"manifest missing at {mpath}", file=sys.stderr)
        return 2
    manifest = read_manifest(mpath)

    expected_total = 0.0
    for s in manifest["segments"]:
        meta = ffprobe_streams(REPO_ROOT / s["clip_path"])
        expected_total += float(meta["format"]["duration"])

    print(f"Stitching {len(manifest['segments'])} clips (expected total ≈ {expected_total:.2f}s)...")
    final_path = stitch_episode(manifest_path=mpath, overwrite=args.force)
    print(f"Stitched: {final_path}")
    validate_stitched_output(final_path, expected_total)
    print("Validation OK")

    manifest = read_manifest(mpath)
    manifest["stitched_path"] = str(final_path.relative_to(REPO_ROOT))
    manifest["validation_status"] = "stitched"
    write_manifest(mpath, manifest)
    print("validation_status=stitched")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    eid = args.episode_id
    mpath = manifest_path_for(eid)
    if not mpath.exists():
        print(f"manifest missing at {mpath}", file=sys.stderr)
        return 2
    manifest = read_manifest(mpath)
    if not manifest.get("stitched_path"):
        print("stitched_path missing — run stitch first.", file=sys.stderr)
        return 2

    final_path = REPO_ROOT / manifest["stitched_path"]
    if not final_path.exists():
        print(f"stitched file missing: {final_path}", file=sys.stderr)
        return 2

    print("Generating SRT from segment timing...")
    srt_path = generate_srt(manifest_path=mpath)
    print(f"  SRT: {srt_path} ({srt_path.stat().st_size} bytes)")

    print("Loading YouTube credentials...")
    creds = load_youtube_credentials()

    visibility = manifest.get("visibility", DEFAULT_VISIBILITY)
    video_id = manifest.get("youtube_id")

    if video_id:
        print(f"manifest already records youtube_id={video_id!r}; skipping video upload.")
        record = verify_youtube_video(credentials=creds, video_id=video_id)
        print(f"  privacyStatus: {record['status']['privacyStatus']!r}")
        print(f"  uploadStatus:  {record['status'].get('uploadStatus')!r}")
        if record["status"]["privacyStatus"] != visibility:
            raise RuntimeError(
                f"privacyStatus on YouTube ({record['status']['privacyStatus']!r}) "
                f"does not match requested {visibility!r}"
            )
    else:
        title = manifest["script"]["title"]
        description = manifest["script"]["description"] + YOUTUBE_DISCLAIMER
        saved_session_uri = manifest.get("youtube_upload_session_uri")
        video_id = None
        if saved_session_uri:
            print(
                f"Found saved resumable session URI; attempting resume from "
                f"{saved_session_uri[:80]}..."
            )
            try:
                video_id = resume_youtube_upload(
                    credentials=creds,
                    video_path=final_path,
                    session_uri=saved_session_uri,
                )
                print(f"  resume succeeded; videoId: {video_id}")
                manifest = read_manifest(mpath)
                manifest.pop("youtube_upload_session_uri", None)
                manifest.pop("youtube_upload_total_bytes", None)
                manifest["youtube_id"] = video_id
                manifest["validation_status"] = "video_uploaded"
                write_manifest(mpath, manifest)
            except Exception as e:
                print(
                    f"  resume failed ({e}); falling back to fresh upload.",
                    file=sys.stderr,
                )
                # Stale session may still be referenced by retries — clear so
                # the fresh upload below stores a new URI cleanly.
                manifest = read_manifest(mpath)
                manifest.pop("youtube_upload_session_uri", None)
                manifest.pop("youtube_upload_total_bytes", None)
                write_manifest(mpath, manifest)

        if not video_id:
            print(f"Uploading video to YouTube ({visibility}): title={title!r}")
            video_id = upload_youtube_video(
                credentials=creds,
                video_path=final_path,
                title=title,
                description=description,
                tags=YOUTUBE_DEFAULT_TAGS,
                visibility=visibility,
                manifest_path=mpath,
            )
            print(f"  videoId: {video_id}")
            manifest = read_manifest(mpath)
            manifest["youtube_id"] = video_id
            manifest["validation_status"] = "video_uploaded"
            write_manifest(mpath, manifest)

        print("Verifying via videos.list...")
        record = verify_youtube_video(credentials=creds, video_id=video_id)
        print(f"  privacyStatus: {record['status']['privacyStatus']!r}")
        print(f"  uploadStatus:  {record['status'].get('uploadStatus')!r}")
        if record["status"]["privacyStatus"] != visibility:
            raise RuntimeError(
                f"privacyStatus on YouTube ({record['status']['privacyStatus']!r}) "
                f"does not match requested {visibility!r}"
            )

    if manifest.get("youtube_caption_id"):
        print("manifest already records youtube_caption_id; skipping caption upload.")
    else:
        print("Uploading caption track...")
        caption_id = upload_youtube_caption(credentials=creds, video_id=video_id, srt_path=srt_path)
        print(f"  caption id: {caption_id}")
        manifest = read_manifest(mpath)
        manifest["youtube_caption_id"] = caption_id
        write_manifest(mpath, manifest)

    manifest = read_manifest(mpath)
    cast = load_cast()
    final_meta = ffprobe_streams(REPO_ROOT / manifest["stitched_path"])
    duration_min = max(1, int(round(float(final_meta["format"]["duration"]) / 60.0)))
    record = EpisodeRecord(
        id=eid,
        episodeNo=int(manifest["episode_no"]),
        title=manifest["script"]["title"],
        date=manifest["run_date"],
        durationMinutes=duration_min,
        youtubeId=manifest["youtube_id"],
        description=manifest["script"]["description"],
        hosts=derive_hosts(EpisodeScript.model_validate(manifest["script"]), cast),
    )
    manifest["episode_record"] = record.model_dump()
    manifest["validation_status"] = "uploaded"
    write_manifest(mpath, manifest)
    print("validation_status=uploaded")
    print(f"Episode record (matches SPA Episode shape): {record.model_dump()}")
    print(f"YouTube URL: https://www.youtube.com/watch?v={video_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Podcast orchestrator (Phase 0b).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show-corpus", help="Print eligible Episode 1 corpus and exit.")

    p_gen = sub.add_parser(
        "generate-script",
        help="Generate the Episode 1 script and write the initial manifest.",
    )
    p_gen.add_argument("--episode-id", default="ep-001")
    p_gen.add_argument("--episode-no", type=int, default=None)
    p_gen.add_argument("--run-date", default=None, help="ISO date; defaults to today.")
    p_gen.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing manifest. Drops all per-segment pipeline state.",
    )

    p_prod = sub.add_parser(
        "produce-segments",
        help="Canary-then-scale TTS + Hedra clip generation for all manifest segments.",
    )
    p_prod.add_argument("--episode-id", default="ep-001")
    p_prod.add_argument(
        "--parallel",
        type=int,
        default=4,
        help="Parallel workers for non-canary segments (default 4).",
    )

    p_stitch = sub.add_parser(
        "stitch",
        help="Concat per-segment clips into final.mp4 + ffprobe validate.",
    )
    p_stitch.add_argument("--episode-id", default="ep-001")
    p_stitch.add_argument("--force", action="store_true", help="Overwrite final.mp4 if it exists.")

    p_up = sub.add_parser(
        "upload",
        help="Generate SRT, upload final.mp4 to YouTube unlisted, verify, upload captions.",
    )
    p_up.add_argument("--episode-id", default="ep-001")

    args = parser.parse_args(argv)
    if args.cmd == "show-corpus":
        return cmd_show_corpus(args)
    if args.cmd == "generate-script":
        return cmd_generate_script(args)
    if args.cmd == "produce-segments":
        return cmd_produce_segments(args)
    if args.cmd == "stitch":
        return cmd_stitch(args)
    if args.cmd == "upload":
        return cmd_upload(args)
    parser.error(f"unknown command {args.cmd!r}")
    return 2
