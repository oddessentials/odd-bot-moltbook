"""Unit tests for src.podcast_hedra_balance_guard.

Covers the credit-floor decision, the billing-endpoint parse, and the
fail-open routing the weekly wrapper relies on:

    PROCEED:credits:<remaining>:<min>   remaining >= floor
    REFUSE:credits:<remaining>:<min>    remaining <  floor
    PROCEED:error:<ExceptionName>       any transport/parse error (fail-open)

Stdlib unittest only — run via:

    .venv/bin/python -m unittest discover -s tests
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

import src.podcast_hedra_balance_guard as guard
from src.podcast_hedra_balance_guard import evaluate, fetch_remaining_credits, main


class TestEvaluate(unittest.TestCase):
    """The pure credit-floor comparison."""

    def test_below_floor_refuses(self):
        self.assertEqual(
            evaluate(remaining=100, min_credits=3000),
            "REFUSE:credits:100:3000",
        )

    def test_zero_refuses(self):
        self.assertEqual(
            evaluate(remaining=0, min_credits=3000),
            "REFUSE:credits:0:3000",
        )

    def test_above_floor_proceeds(self):
        self.assertEqual(
            evaluate(remaining=5000, min_credits=3000),
            "PROCEED:credits:5000:3000",
        )

    def test_exact_floor_proceeds(self):
        # Boundary: remaining == floor is enough (>=).
        self.assertEqual(
            evaluate(remaining=3000, min_credits=3000),
            "PROCEED:credits:3000:3000",
        )


class _FakeResp:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise guard.requests.HTTPError("402 Payment Required")

    def json(self):
        return self._payload


class TestFetchRemainingCredits(unittest.TestCase):
    """The billing-endpoint call + payload parsing (network mocked)."""

    def test_happy_path_returns_remaining(self):
        with mock.patch.object(guard, "load_hedra_key", return_value="k"), \
             mock.patch.object(
                 guard.requests, "get",
                 return_value=_FakeResp(
                     {"remaining": 4200, "used": 1, "expiring": 0}),
             ) as get:
            self.assertEqual(fetch_remaining_credits(), 4200)
            # Sanity: documented endpoint + api-key header.
            args, kwargs = get.call_args
            self.assertEqual(args[0], guard.CREDITS_ENDPOINT)
            self.assertEqual(kwargs["headers"], {"x-api-key": "k"})

    def test_missing_remaining_raises(self):
        with mock.patch.object(guard, "load_hedra_key", return_value="k"), \
             mock.patch.object(
                 guard.requests, "get",
                 return_value=_FakeResp({"used": 1}),
             ):
            with self.assertRaises(ValueError):
                fetch_remaining_credits()

    def test_bool_remaining_raises(self):
        # A JSON bool must not be accepted as an int credit count.
        with mock.patch.object(guard, "load_hedra_key", return_value="k"), \
             mock.patch.object(
                 guard.requests, "get",
                 return_value=_FakeResp({"remaining": True}),
             ):
            with self.assertRaises(ValueError):
                fetch_remaining_credits()

    def test_non_2xx_raises(self):
        with mock.patch.object(guard, "load_hedra_key", return_value="k"), \
             mock.patch.object(
                 guard.requests, "get",
                 return_value=_FakeResp({}, status_ok=False),
             ):
            with self.assertRaises(guard.requests.HTTPError):
                fetch_remaining_credits()

    def test_float_remaining_raises(self):
        with mock.patch.object(guard, "load_hedra_key", return_value="k"), \
             mock.patch.object(
                 guard.requests, "get",
                 return_value=_FakeResp({"remaining": 1.5}),
             ):
            with self.assertRaises(ValueError):
                fetch_remaining_credits()

    def test_string_remaining_raises(self):
        with mock.patch.object(guard, "load_hedra_key", return_value="k"), \
             mock.patch.object(
                 guard.requests, "get",
                 return_value=_FakeResp({"remaining": "4200"}),
             ):
            with self.assertRaises(ValueError):
                fetch_remaining_credits()

    def test_list_payload_raises(self):
        # A non-dict JSON body (list) must raise, not silently pass.
        with mock.patch.object(guard, "load_hedra_key", return_value="k"), \
             mock.patch.object(
                 guard.requests, "get",
                 return_value=_FakeResp([1, 2, 3]),
             ):
            with self.assertRaises(AttributeError):
                fetch_remaining_credits()

    def test_negative_remaining_returns_int(self):
        # Negative is a valid int -> passes through; evaluate() then REFUSEs.
        with mock.patch.object(guard, "load_hedra_key", return_value="k"), \
             mock.patch.object(
                 guard.requests, "get",
                 return_value=_FakeResp({"remaining": -5}),
             ):
            self.assertEqual(fetch_remaining_credits(), -5)


class TestMainRouting(unittest.TestCase):
    """The bash-facing CLI: stdout prefix + fail-open routing."""

    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(argv)
        return rc, buf.getvalue().strip()

    def test_proceed_when_sufficient(self):
        with mock.patch.object(guard, "fetch_remaining_credits",
                               return_value=5000):
            rc, out = self._run(["--min", "3000"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "PROCEED:credits:5000:3000")

    def test_refuse_when_insufficient(self):
        with mock.patch.object(guard, "fetch_remaining_credits",
                               return_value=100):
            rc, out = self._run(["--min", "3000"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "REFUSE:credits:100:3000")

    def test_network_error_fails_open(self):
        with mock.patch.object(guard, "fetch_remaining_credits",
                               side_effect=guard.requests.ConnectionError("x")):
            rc, out = self._run(["--min", "3000"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "PROCEED:error:ConnectionError")

    def test_parse_error_fails_open(self):
        with mock.patch.object(guard, "fetch_remaining_credits",
                               side_effect=ValueError("bad shape")):
            rc, out = self._run(["--min", "3000"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "PROCEED:error:ValueError")


class TestLoadKey(unittest.TestCase):
    def test_missing_key_raises_value_error(self):
        # Locks the load-bearing invariant: the standalone loader raises
        # ValueError (caught by fail-open), NOT the canonical SystemExit.
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / ".keys"
            p.write_text("Some Other Key: xyz\n")
            with mock.patch.object(guard, "KEYS_FILE", p):
                with self.assertRaises(ValueError):
                    guard.load_hedra_key()


if __name__ == "__main__":
    unittest.main()
