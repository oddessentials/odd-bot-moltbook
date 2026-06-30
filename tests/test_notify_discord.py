"""Functional test for scripts/notify_discord.sh.

Sources the helper in a real bash (under `set -euo pipefail`, as the
wrappers do) and asserts:
  - it builds a valid Discord JSON body {"content": "..."} with correct
    escaping of quotes and backslashes, and bypasses the proxy;
  - it is a silent no-op when no webhook is configured.

Network-free: a fake `curl` shimmed onto PATH captures the argv the helper
would have sent, so the test never makes an HTTP call.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "notify_discord.sh"

# Fake curl: dump argv (one per line) to $CURL_CAPTURE and exit 0.
_FAKE_CURL = "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$CURL_CAPTURE\"\n"


@unittest.skipUnless(shutil.which("bash"), "bash required")
class TestNotifyDiscord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bindir = Path(self.tmp) / "bin"
        self.bindir.mkdir()
        fake = self.bindir / "curl"
        fake.write_text(_FAKE_CURL)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self.capture = Path(self.tmp) / "capture.txt"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, message, env_extra):
        env = dict(os.environ)
        env.pop("OBSERVATORY_DISCORD_WEBHOOK_URL", None)  # control it via env_extra only
        env["PATH"] = f"{self.bindir}:{env['PATH']}"
        env["CURL_CAPTURE"] = str(self.capture)
        env["MSG"] = message
        env.update(env_extra)
        script = f'set -euo pipefail; . "{HELPER}"; notify_discord "$MSG"'
        return subprocess.run(
            ["bash", "-c", script],
            env=env, capture_output=True, text=True, timeout=20,
        )

    def test_posts_escaped_json_when_webhook_set(self):
        # Message with a double-quote and a backslash — the JSON-breaking case.
        proc = self._run(
            'he said "hi" \\back',
            {"OBSERVATORY_DISCORD_WEBHOOK_URL": "http://127.0.0.1:9/hook"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.capture.exists(), "curl was not invoked")
        argv = self.capture.read_text().splitlines()
        payloads = [a for a in argv if a.startswith("{")]
        self.assertEqual(len(payloads), 1, argv)
        body = json.loads(payloads[0])  # must be valid JSON
        self.assertEqual(body["content"], 'he said "hi" \\back')
        self.assertIn("--noproxy", argv)  # proxy bypass present

    def test_noop_when_no_webhook(self):
        # No env var + a HOME without the key file => silent no-op, no curl.
        proc = self._run("anything", {"HOME": self.tmp})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.capture.exists(), "curl ran with no webhook configured")

    def test_survives_unset_home(self):
        # Under `set -u`, an unset HOME must NOT abort the caller (the
        # helper's "always returns 0" contract). Reproduces the bug fixed
        # by ${HOME:-} in notify_discord.sh.
        env = dict(os.environ)
        env.pop("OBSERVATORY_DISCORD_WEBHOOK_URL", None)
        env.pop("HOME", None)
        env["PATH"] = f"{self.bindir}:{env['PATH']}"
        env["CURL_CAPTURE"] = str(self.capture)
        env["MSG"] = "x"
        script = f'set -euo pipefail; . "{HELPER}"; notify_discord "$MSG"; echo SURVIVED'
        proc = subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SURVIVED", proc.stdout)
        self.assertFalse(self.capture.exists())


if __name__ == "__main__":
    unittest.main()
