"""Unit tests for src.ca_bundle.

Pins the trust-store invariant for the weekly podcast wrapper: the
combined PEM must contain certifi public roots AND the mitm CA when the
mitm CA file is present, so traffic that bypasses the proxy
(NO_PROXY-listed services like ElevenLabs / Hedra / googleapis / S3)
validates against public roots and traffic that goes through the proxy
validates against the mitm-substituted cert in the same `SSL_CERT_FILE`.

Failure mode locked down here: 2026-05-10 09:00 ET podcast run failed
with `SSLCertVerificationError` because the wrapper exported the mitm
CA alone — TTS calls to ElevenLabs (correctly bypassed via NO_PROXY)
saw the real public-CA-signed cert and could not validate it.

Stdlib unittest only — run via:

    .venv/bin/python -m unittest discover -s tests
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import certifi

from src.ca_bundle import build_combined_bundle


class TestBuildCombinedBundle(unittest.TestCase):
    def test_certifi_only_when_mitm_ca_absent(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mitm = tdp / "does-not-exist.pem"
            out = tdp / "ca-bundle.pem"

            result = build_combined_bundle(
                output_path=out, mitm_ca_path=mitm,
            )

            self.assertEqual(result, out)
            self.assertTrue(out.exists())

            content = out.read_bytes()
            certifi_content = Path(certifi.where()).read_bytes()
            self.assertTrue(
                content.startswith(certifi_content),
                "bundle must begin with certifi public roots",
            )
            # No mitm appended when source missing.
            extra = content[len(certifi_content):]
            self.assertIn(extra, (b"", b"\n"))

    def test_combined_when_mitm_ca_present(self):
        # Both content sources must end up in the combined bundle. This
        # is the load-bearing invariant: ElevenLabs traffic needs public
        # roots AND mitm-routed Anthropic traffic needs the mitm CA.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mitm = tdp / "mitm.pem"
            mitm_marker = b"FAKE_MITM_CA_MARKER_FOR_TEST"
            mitm.write_bytes(
                b"-----BEGIN CERTIFICATE-----\n"
                + mitm_marker
                + b"\n-----END CERTIFICATE-----\n"
            )
            out = tdp / "ca-bundle.pem"

            build_combined_bundle(output_path=out, mitm_ca_path=mitm)

            content = out.read_bytes()
            certifi_content = Path(certifi.where()).read_bytes()
            self.assertTrue(
                content.startswith(certifi_content),
                "certifi roots must come first",
            )
            self.assertIn(
                mitm_marker, content,
                "mitm CA payload must appear in combined bundle",
            )

    def test_inserts_newline_between_chunks_if_certifi_lacks_trailing_newline(
        self,
    ):
        # Defensive: if the certifi source somehow lacks a trailing
        # newline, the combined bundle must still parse cleanly. We
        # simulate this by passing a synthetic certifi bundle path.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            fake_certifi = tdp / "fake-certifi.pem"
            fake_certifi.write_bytes(
                b"-----BEGIN CERTIFICATE-----\nFAKE_CERTIFI_NO_NEWLINE"
                b"\n-----END CERTIFICATE-----"  # no trailing newline
            )
            mitm = tdp / "mitm.pem"
            mitm.write_bytes(b"-----BEGIN CERTIFICATE-----\nMITM\n-----END CERTIFICATE-----\n")
            out = tdp / "ca-bundle.pem"

            build_combined_bundle(
                output_path=out,
                mitm_ca_path=mitm,
                certifi_bundle_path=fake_certifi,
            )

            content = out.read_bytes()
            # End-of-certifi must be followed by a newline before the
            # mitm BEGIN line, otherwise parsers see a glued line.
            self.assertIn(b"-----END CERTIFICATE-----\n-----BEGIN", content)

    def test_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mitm = tdp / "missing.pem"
            out = tdp / "nested" / "subdir" / "ca-bundle.pem"

            build_combined_bundle(output_path=out, mitm_ca_path=mitm)
            self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main()
