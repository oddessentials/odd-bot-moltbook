#!/usr/bin/env python3
"""One-shot YouTube OAuth consent capture.

Runs the Google installed-app flow on the Mac mini, captures a refresh token,
and verifies access against the @odd_essentials channel via channels.list +
(optionally) videos.list. Writes the resulting tokens back into the gitignored
`.keys` blob alongside the existing client_secret JSON.

Re-run safety: re-running overwrites the youtube_tokens section in `.keys`.
The Google consent screen for project oddbot-483603 is Published to production,
so the captured refresh token is stable (no 7-day Testing-mode expiry).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    # captions.insert requires force-ssl to manage caption tracks on
    # videos owned by the authenticated channel.
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
KEYS_FILE = REPO_ROOT / ".keys"
CLIENT_JSON_PATTERN = re.compile(r'(\{"installed":\{[^\n]+\}\})')
TOKEN_SECTION_HEADER = "# youtube_tokens"


def load_client_config() -> dict:
    text = KEYS_FILE.read_text()
    match = CLIENT_JSON_PATTERN.search(text)
    if not match:
        raise SystemExit(
            "No installed-app client JSON found in .keys. Expected a single-line "
            "blob shaped like {\"installed\": {...}}."
        )
    return json.loads(match.group(1))


def write_tokens(creds) -> None:
    text = KEYS_FILE.read_text().rstrip()
    section_marker = f"\n\n{TOKEN_SECTION_HEADER}\n"
    if TOKEN_SECTION_HEADER in text:
        text = text.split(f"\n\n{TOKEN_SECTION_HEADER}", 1)[0].rstrip()
    payload = {
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "scopes": list(creds.scopes or SCOPES),
    }
    KEYS_FILE.write_text(text + section_marker + json.dumps(payload, indent=2) + "\n")


def main() -> int:
    client_config = load_client_config()
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    print("Opening browser for Google consent. Authenticate as the @odd_essentials owner.")
    creds = flow.run_local_server(
        port=0,
        prompt="consent",
        access_type="offline",
        open_browser=True,
    )
    if not creds.refresh_token:
        raise SystemExit(
            "Consent flow returned no refresh token. Did you previously grant access? "
            "Revoke at https://myaccount.google.com/permissions and retry."
        )

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    channels_resp = youtube.channels().list(part="id,snippet,contentDetails", mine=True).execute()
    items = channels_resp.get("items", [])
    if not items:
        raise SystemExit("channels.list(mine=True) returned no channels — wrong account?")
    channel = items[0]
    cid = channel["id"]
    title = channel["snippet"]["title"]
    custom_url = channel["snippet"].get("customUrl", "<none>")
    uploads_playlist = channel["contentDetails"]["relatedPlaylists"]["uploads"]
    print(f"Authenticated channel: id={cid} title={title!r} customUrl={custom_url!r}")
    print(f"Uploads playlist: {uploads_playlist}")

    pl_resp = (
        youtube.playlistItems()
        .list(part="contentDetails", playlistId=uploads_playlist, maxResults=5)
        .execute()
    )
    video_ids = [it["contentDetails"]["videoId"] for it in pl_resp.get("items", [])]
    if video_ids:
        v_resp = youtube.videos().list(part="id,snippet,status", id=",".join(video_ids)).execute()
        print(f"videos.list returned {len(v_resp.get('items', []))} item(s):")
        for v in v_resp.get("items", []):
            print(f"  - {v['id']} status={v['status'].get('privacyStatus')} title={v['snippet']['title']!r}")
    else:
        print("Channel has zero existing uploads. videos.list shape verified via empty result path.")
        empty = youtube.videos().list(part="id", id="").execute()
        if "items" not in empty:
            raise SystemExit("videos.list shape unexpected.")
        print("videos.list call shape OK (empty id returned empty items list).")

    write_tokens(creds)
    print(f"Refresh token persisted to {KEYS_FILE}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
