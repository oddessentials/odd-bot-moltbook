"""YouTube upload + caption + verify, using OAuth refresh-token credentials.

Visibility is locked to "unlisted" for Phase 0; the public flip belongs in
Phase 2's publish-event work.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_VISIBILITY,
    YOUTUBE_CATEGORY_ID,
    YOUTUBE_DEFAULT_LANGUAGE,
)


def upload_youtube_video(
    *,
    credentials,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    visibility: str = DEFAULT_VISIBILITY,
) -> str:
    """Resumable upload to YouTube. Returns the videoId."""
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
    media = MediaFileUpload(
        str(video_path), chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4"
    )
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"    upload progress: {int(status.progress() * 100)}%")
    return response["id"]


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
