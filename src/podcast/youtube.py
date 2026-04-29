"""YouTube upload + caption + verify, using OAuth refresh-token credentials.

Visibility is locked to "unlisted" for Phase 0; the public flip belongs in
Phase 2's publish-event work.

Resumable-upload session URI persistence: when `upload_youtube_video` is
called with a `manifest_path`, the resumable session URI is captured the
moment the SDK establishes it and written to the manifest. If the process
dies mid-upload, the next invocation can use `resume_youtube_upload` to
finish from the saved offset instead of restarting at byte 0.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import requests

from .config import (
    DEFAULT_VISIBILITY,
    YOUTUBE_CATEGORY_ID,
    YOUTUBE_DEFAULT_LANGUAGE,
)
from .manifest import read_manifest, write_manifest

UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024


def _persist_session_uri(manifest_path: Path, session_uri: str, total_bytes: int) -> None:
    m = read_manifest(manifest_path)
    m["youtube_upload_session_uri"] = session_uri
    m["youtube_upload_total_bytes"] = total_bytes
    write_manifest(manifest_path, m)


def _clear_session_uri(manifest_path: Path) -> None:
    m = read_manifest(manifest_path)
    m.pop("youtube_upload_session_uri", None)
    m.pop("youtube_upload_total_bytes", None)
    write_manifest(manifest_path, m)


def _refresh_credentials_if_needed(credentials) -> None:
    if not credentials.valid:
        from google.auth.transport.requests import Request as AuthRequest
        credentials.refresh(AuthRequest())


def upload_youtube_video(
    *,
    credentials,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    visibility: str = DEFAULT_VISIBILITY,
    manifest_path: Path | None = None,
) -> str:
    """Resumable upload to YouTube. Returns the videoId.

    When `manifest_path` is provided, persists the resumable session URI to
    the manifest after the SDK establishes the session (i.e., after the
    first `next_chunk()` call). The URI is cleared from the manifest on
    successful completion. A process crash mid-upload leaves the URI in
    the manifest so the next run can hand it to `resume_youtube_upload`.
    """
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": YOUTUBE_CATEGORY_ID,
            "defaultLanguage": YOUTUBE_DEFAULT_LANGUAGE,
            "defaultAudioLanguage": YOUTUBE_DEFAULT_LANGUAGE,
        },
        "status": {
            "privacyStatus": visibility,
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
        },
    }
    total_bytes = video_path.stat().st_size
    media = MediaFileUpload(
        str(video_path),
        chunksize=UPLOAD_CHUNK_SIZE,
        resumable=True,
        mimetype="video/mp4",
    )
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    uri_saved = False
    while response is None:
        status, response = request.next_chunk()
        if (
            manifest_path is not None
            and not uri_saved
            and getattr(request, "resumable_uri", None)
        ):
            _persist_session_uri(manifest_path, request.resumable_uri, total_bytes)
            uri_saved = True
        if status:
            print(f"    upload progress: {int(status.progress() * 100)}%")
    if manifest_path is not None:
        _clear_session_uri(manifest_path)
    return response["id"]


def resume_youtube_upload(
    *,
    credentials,
    video_path: Path,
    session_uri: str,
    chunk_size: int = UPLOAD_CHUNK_SIZE,
) -> str:
    """Resume a previously-established resumable upload by URI.

    Implements the resumable-upload protocol directly so we don't need
    googleapiclient internals to wire a saved session into a fresh
    HttpRequest. The session URI carries all the upload metadata
    (snippet, status) that was negotiated when the session was created;
    we only need to push the remaining bytes.

    Probes the session with `Content-Range: bytes */<total>` to discover
    how much has already been uploaded:
      - 200/201 → upload was already complete; response body is the video
        resource. Return videoId.
      - 308    → partial. Range header tells us where to resume.
      - other  → raise; caller falls back to fresh upload.
    """
    _refresh_credentials_if_needed(credentials)
    total = video_path.stat().st_size
    auth = {"Authorization": f"Bearer {credentials.token}"}

    probe = requests.put(
        session_uri,
        headers={
            **auth,
            "Content-Length": "0",
            "Content-Range": f"bytes */{total}",
        },
        timeout=60,
    )
    if probe.status_code in (200, 201):
        return probe.json()["id"]
    if probe.status_code != 308:
        raise RuntimeError(
            f"resume probe returned {probe.status_code}: {probe.text[:200]!r}"
        )

    range_header = probe.headers.get("Range", "")
    if not range_header:
        start = 0
    else:
        m = re.match(r"bytes=0-(\d+)", range_header)
        if not m:
            raise RuntimeError(f"unparseable Range header: {range_header!r}")
        start = int(m.group(1)) + 1

    print(f"    resuming from byte {start}/{total} ({int(start / total * 100)}% done)")
    with video_path.open("rb") as f:
        while start < total:
            f.seek(start)
            end = min(start + chunk_size, total) - 1
            chunk = f.read(end - start + 1)
            resp = requests.put(
                session_uri,
                data=chunk,
                headers={
                    **auth,
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{total}",
                },
                timeout=300,
            )
            if resp.status_code in (200, 201):
                return resp.json()["id"]
            if resp.status_code != 308:
                raise RuntimeError(
                    f"resume PUT at {start}-{end} returned {resp.status_code}: "
                    f"{resp.text[:200]!r}"
                )
            print(f"    resume progress: {int((end + 1) / total * 100)}%")
            start = end + 1
    raise RuntimeError("resume loop exited without 200 — server should have terminated")


def upload_youtube_caption(
    *,
    credentials,
    video_id: str,
    srt_path: Path,
    language: str = YOUTUBE_DEFAULT_LANGUAGE,
) -> str:
    """Upload an SRT as a caption track for `video_id`. Returns the caption id."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    body = {
        "snippet": {
            "videoId": video_id,
            "language": language,
            "name": "English",
            "isDraft": False,
        }
    }
    media = MediaFileUpload(str(srt_path), mimetype="application/octet-stream", resumable=False)
    resp = youtube.captions().insert(part="snippet", body=body, media_body=media).execute()
    return resp["id"]


def verify_youtube_video(*, credentials, video_id: str) -> dict[str, Any]:
    """Confirm the upload via videos.list. Returns the snippet+status payload."""
    from googleapiclient.discovery import build
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    resp = youtube.videos().list(part="id,snippet,status", id=video_id).execute()
    items = resp.get("items", [])
    if not items:
        raise RuntimeError(f"videos.list found no item for id={video_id}")
    return items[0]
