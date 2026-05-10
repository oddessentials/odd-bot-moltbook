"""Combined CA bundle helper for wrapper-managed TLS trust.

The weekly podcast wrapper routes Anthropic + Moltbook traffic through
the local mitmproxy (which signs substituted certs with its own CA) but
exempts ElevenLabs / Hedra / googleapis / S3 from the proxy via NO_PROXY
because those services use strict cert validation. Pointing
`SSL_CERT_FILE` at the mitm CA alone breaks the bypassed services
(public-CA-signed certs cannot be validated against a 1-CA bundle);
pointing it at certifi alone breaks mitm-routed traffic.

This module materializes a combined PEM (certifi public roots + mitm CA
when present) at a deterministic cache path and prints that path. The
wrapper exports `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` to it, so one
trust store satisfies both paths.

Wrapper usage (bash):

    BUNDLE="$("$REPO_ROOT/.venv/bin/python" -m src.ca_bundle)"
    export SSL_CERT_FILE="$BUNDLE"
    export REQUESTS_CA_BUNDLE="$BUNDLE"

Pure-ish: filesystem read + write. No network, no env mutation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import certifi

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MITM_CA = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
DEFAULT_OUTPUT = REPO_ROOT / "data" / ".ca-bundle.pem"


def build_combined_bundle(
    output_path: Path = DEFAULT_OUTPUT,
    mitm_ca_path: Path = DEFAULT_MITM_CA,
    certifi_bundle_path: Path | None = None,
) -> Path:
    """Write `<certifi roots> + <mitm CA if present>` to `output_path`.

    Returns the output path. Inserts a trailing newline between chunks
    so concatenated PEM blocks parse cleanly even if either source lacks
    a final newline. If `mitm_ca_path` does not exist, the bundle is
    certifi-only — direct upstream calls still validate; mitm-routed
    calls would fail, but the wrapper's invariant is that mitm is up
    when the proxy env vars are set.
    """
    if certifi_bundle_path is None:
        certifi_bundle_path = Path(certifi.where())

    output_path.parent.mkdir(parents=True, exist_ok=True)

    chunks: list[bytes] = [certifi_bundle_path.read_bytes()]
    if not chunks[-1].endswith(b"\n"):
        chunks.append(b"\n")
    if mitm_ca_path.exists():
        chunks.append(mitm_ca_path.read_bytes())
        if not chunks[-1].endswith(b"\n"):
            chunks.append(b"\n")

    output_path.write_bytes(b"".join(chunks))
    return output_path


def main(argv: list[str] | None = None) -> int:
    path = build_combined_bundle()
    sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
