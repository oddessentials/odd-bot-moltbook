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
    SEGMENT_MAX_ATTEMPTS,
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
    acquire_run_lock,
    advance_validation_status,
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
from .segments import process_segment_with_retry
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
    1..N-1 run with the same gates. Each segment runs under a bounded
    retry budget (default SEGMENT_MAX_ATTEMPTS); any segment failure
    after the budget is exhausted aborts the run with the manifest left
    in whatever partial state it reached — the next invocation reuses
    already-complete segments.
    """
    eid = args.episode_id
    mpath = manifest_path_for(eid)
    if not mpath.exists():
        print(f"manifest missing at {mpath} — run generate-script first.", file=sys.stderr)
        return 2

    if args.max_attempts is None:
        args.max_attempts = SEGMENT_MAX_ATTEMPTS

    manifest = read_manifest(mpath)
    segments = manifest["segments"]
    n = len(segments)
    print(f"Producing {n} segment(s) for episode {eid} (max_attempts={args.max_attempts})")

    cast = load_cast()
    e_key = load_elevenlabs_key()
    h_session = hedra_session(load_hedra_key())

    print("Canary: segment 0...")
    process_segment_with_retry(
        manifest_path=mpath, idx=0, cast=cast,
        elevenlabs_key=e_key, hedra_session=h_session,
        max_attempts=args.max_attempts,
    )
    print("Canary green. Scaling to remaining segments in parallel...")

    parallel_workers = min(args.parallel, max(1, n - 1))
    if parallel_workers <= 1 or n <= 2:
        for idx in range(1, n):
            process_segment_with_retry(
                manifest_path=mpath, idx=idx, cast=cast,
                elevenlabs_key=e_key, hedra_session=h_session,
                max_attempts=args.max_attempts,
            )
    else:
        with ThreadPoolExecutor(max_workers=parallel_workers) as ex:
            futures = {
                ex.submit(
                    process_segment_with_retry,
                    manifest_path=mpath, idx=idx, cast=cast,
                    elevenlabs_key=e_key, hedra_session=h_session,
                    max_attempts=args.max_attempts,
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

    landed = advance_validation_status(mpath, "segments_complete")
    print(f"All {n} segments complete. validation_status={landed}")
    return 0


def cmd_stitch(args: argparse.Namespace) -> int:
    eid = args.episode_id
    mpath = manifest_path_for(eid)
    if not mpath.exists():
        print(f"manifest missing at {mpath}", file=sys.stderr)
        return 2
    manifest = read_manifest(mpath)

    completed_states = ("stitched", "video_uploaded", "uploaded")
    stitched_path_str = manifest.get("stitched_path")
    if (
        not args.force
        and manifest.get("validation_status") in completed_states
        and stitched_path_str
        and (REPO_ROOT / stitched_path_str).exists()
    ):
        print(
            f"final.mp4 already stitched at {stitched_path_str} "
            f"(validation_status={manifest.get('validation_status')!r}); skipping. "
            "Pass --force to re-stitch."
        )
        return 0

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
    write_manifest(mpath, manifest)
    landed = advance_validation_status(mpath, "stitched")
    print(f"validation_status={landed}")
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
            write_manifest(mpath, manifest)
            advance_validation_status(mpath, "video_uploaded")

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
    write_manifest(mpath, manifest)
    landed = advance_validation_status(mpath, "uploaded")
    print(f"validation_status={landed}")
    print(f"Episode record (matches SPA Episode shape): {record.model_dump()}")
    print(f"YouTube URL: https://www.youtube.com/watch?v={video_id}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Walk the full pipeline and skip phases that are already done.

    Single entry point for the steady-state invocation: idempotent across
    all four phases (script → segments → stitch → upload). The operator
    can run this from any partial state and the engine catches up to a
    fully published episode (still unlisted in Phase 0; the public flip
    is Phase 2's publish-event work).

    Lock is held for the entire pipeline run via main()'s dispatcher
    wrapper, so per-phase calls never re-acquire.
    """
    eid = args.episode_id
    mpath = manifest_path_for(eid)

    # Phase 1: generate-script (refuses to clobber unless --force).
    if mpath.exists() and not args.force:
        print(f"[run] manifest exists at {mpath}; skipping generate-script.")
    else:
        rc = cmd_generate_script(
            argparse.Namespace(
                episode_id=eid,
                episode_no=args.episode_no,
                run_date=args.run_date,
                force=args.force,
            )
        )
        if rc != 0:
            return rc

    # Phase 2: produce-segments. Always invoked — even if validation_status
    # is already segments_complete or beyond, we still need each segment's
    # process_segment idempotency-skip path to re-run validate_segment_outputs
    # against the artifacts on disk. Skipping based on validation_status
    # alone would let an out-of-band corruption (file deleted, partial
    # overwrite, manifest tampering) bypass the gate. The re-validation is
    # cheap (~100ms per already-complete segment) and process_segment
    # short-circuits without touching ElevenLabs/Hedra when the gates pass.
    rc = cmd_produce_segments(
        argparse.Namespace(
            episode_id=eid,
            parallel=args.parallel,
            max_attempts=args.max_attempts,
        )
    )
    if rc != 0:
        return rc

    # Phase 3: stitch. cmd_stitch is idempotent for non-forced runs.
    rc = cmd_stitch(argparse.Namespace(episode_id=eid, force=False))
    if rc != 0:
        return rc

    # Phase 4: upload. Already idempotent on youtube_id + youtube_caption_id.
    return cmd_upload(argparse.Namespace(episode_id=eid))


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
    p_prod.add_argument(
        "--max-attempts",
        type=int,
        default=None,
        help="Per-segment retry budget (default: SEGMENT_MAX_ATTEMPTS = 3).",
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

    p_run = sub.add_parser(
        "run",
        help=(
            "Walk the full pipeline (generate-script → produce-segments → "
            "stitch → upload), skipping phases that are already complete. "
            "Idempotent — safe to invoke from any partial state."
        ),
    )
    p_run.add_argument("--episode-id", default="ep-001")
    p_run.add_argument("--episode-no", type=int, default=None)
    p_run.add_argument("--run-date", default=None)
    p_run.add_argument("--parallel", type=int, default=4)
    p_run.add_argument("--max-attempts", type=int, default=None)
    p_run.add_argument(
        "--force",
        action="store_true",
        help="Wipe stale episode dir and restart from scratch.",
    )

    args = parser.parse_args(argv)
    if args.cmd == "show-corpus":
        # Read-only diagnostic — does not contend with concurrent runs.
        return cmd_show_corpus(args)

    locked_dispatch = {
        "generate-script": cmd_generate_script,
        "produce-segments": cmd_produce_segments,
        "stitch": cmd_stitch,
        "upload": cmd_upload,
        "run": cmd_run,
    }
    handler = locked_dispatch.get(args.cmd)
    if handler is None:
        parser.error(f"unknown command {args.cmd!r}")
        return 2
    try:
        with acquire_run_lock():
            return handler(args)
    except BlockingIOError:
        print(
            "Another podcast run holds the lock. Exiting cleanly to let the "
            "sibling finish.",
            file=sys.stderr,
        )
        return 0
