"""Unit tests for src.podcast_elevenlabs_balance_guard.

Mirrors the Hedra guard's tests: the floor decision, the subscription
parse (remaining = character_limit - character_count), and fail-open
routing. Stdlib unittest only — run via:

    .venv/bin/python -m unittest discover -s tests
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

import src.podcast_elevenlabs_balance_guard as guard
from src.podcast_elevenlabs_balance_guard import (
    evaluate,
    fetch_remaining_chars,
    main,
)


class TestEvaluate(unittest.TestCase):
    def test_below_floor_refuses(self):
        self.assertEqual(
            evaluate(remaining=100, min_chars=5000), "REFUSE:chars:100:5000"
        )

    def test_zero_refuses(self):
        self.assertEqual(
            evaluate(remaining=0, min_chars=5000), "REFUSE:chars:0:5000"
        )

    def test_above_floor_proceeds(self):
        self.assertEqual(
            evaluate(remaining=9000, min_chars=5000), "PROCEED:chars:9000:5000"
        )

    def test_exact_floor_proceeds(self):
        self.assertEqual(
            evaluate(remaining=5000, min_chars=5000), "PROCEED:chars:5000:5000"
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


class TestFetchRemainingChars(unittest.TestCase):
    def test_happy_path_returns_remaining(self):
        with mock.patch.object(guard, "load_elevenlabs_key", return_value="k"), \
             mock.patch.object(
                 guard.requests, "get",
                 return_value=_FakeResp(
                     {"character_count": 1000, "character_limit": 9000}),
             ) as get:
            self.assertEqual(fetch_remaining_chars(), 8000)
            args, kwargs = get.call_args
            self.assertEqual(args[0], guard.SUBSCRIPTION_ENDPOINT)
            self.assertEqual(kwargs["headers"], {"xi-api-key": "k"})

    def test_missing_count_raises(self):
        with mock.patch.object(guard, "load_elevenlabs_key", return_value="k"), \
             mock.patch.object(
                 guard.requests, "get",
                 return_value=_FakeResp({"character_limit": 9000}),
             ):
            with self.assertRaises(ValueError):
                fetch_remaining_chars()

    def test_missing_limit_raises(self):
        with mock.patch.object(guard, "load_elevenlabs_key", return_value="k"), \
             mock.patch.object(
                 guard.requests, "get",
                 return_value=_FakeResp({"character_count": 1000}),
             ):
            with self.assertRaises(ValueError):
                fetch_remaining_chars()

    def test_bool_field_raises(self):
        with mock.patch.object(guard, "load_elevenlabs_key", return_value="k"), \
             mock.patch.object(
                 guard.requests, "get",
                 return_value=_FakeResp(
                     {"character_count": True, "character_limit": 9000}),
             ):
            with self.assertRaises(ValueError):
                fetch_remaining_chars()

    def test_non_2xx_raises(self):
        with mock.patch.object(guard, "load_elevenlabs_key", return_value="k"), \
             mock.patch.object(
                 guard.requests, "get",
                 return_value=_FakeResp({}, status_ok=False),
             ):
            with self.assertRaises(guard.requests.HTTPError):
                fetch_remaining_chars()

    def test_float_field_raises(self):
        with mock.patch.object(guard, "load_elevenlabs_key", return_value="k"), \
             mock.patch.object(
                 guard.requests, "get",
                 return_value=_FakeResp(
                     {"character_count": 1.5, "character_limit": 9000}),
             ):
            with self.assertRaises(ValueError):
                fetch_remaining_chars()

    def test_string_field_raises(self):
        with mock.patch.object(guard, "load_elevenlabs_key", return_value="k"), \
             mock.patch.object(
                 guard.requests, "get",
                 return_value=_FakeResp(
                     {"character_count": "1", "character_limit": 9000}),
             ):
            with self.assertRaises(ValueError):
                fetch_remaining_chars()

    def test_bool_limit_raises(self):
        # Pin that BOTH fields are checked — a regression dropping `limit`
        # from the validation loop must fail here.
        with mock.patch.object(guard, "load_elevenlabs_key", return_value="k"), \
             mock.patch.object(
                 guard.requests, "get",
                 return_value=_FakeResp(
                     {"character_count": 1000, "character_limit": True}),
             ):
            with self.assertRaises(ValueError):
                fetch_remaining_chars()

    def test_list_payload_raises(self):
        with mock.patch.object(guard, "load_elevenlabs_key", return_value="k"), \
             mock.patch.object(
                 guard.requests, "get",
                 return_value=_FakeResp([1, 2, 3]),
             ):
            with self.assertRaises(AttributeError):
                fetch_remaining_chars()

    def test_overage_returns_negative(self):
        # count > limit (overage/usage-based) -> negative remaining passes
        # through as an int; evaluate() then REFUSEs. Documents the assumption.
        with mock.patch.object(guard, "load_elevenlabs_key", return_value="k"), \
             mock.patch.object(
                 guard.requests, "get",
                 return_value=_FakeResp(
                     {"character_count": 9500, "character_limit": 9000}),
             ):
            self.assertEqual(fetch_remaining_chars(), -500)


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
                    guard.load_elevenlabs_key()


class TestMainRouting(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(argv)
        return rc, buf.getvalue().strip()

    def test_proceed_when_sufficient(self):
        with mock.patch.object(guard, "fetch_remaining_chars",
                               return_value=9000):
            rc, out = self._run(["--min", "5000"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "PROCEED:chars:9000:5000")

    def test_refuse_when_insufficient(self):
        with mock.patch.object(guard, "fetch_remaining_chars",
                               return_value=100):
            rc, out = self._run(["--min", "5000"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "REFUSE:chars:100:5000")

    def test_network_error_fails_open(self):
        with mock.patch.object(guard, "fetch_remaining_chars",
                               side_effect=guard.requests.ConnectionError("x")):
            rc, out = self._run(["--min", "5000"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "PROCEED:error:ConnectionError")

    def test_parse_error_fails_open(self):
        with mock.patch.object(guard, "fetch_remaining_chars",
                               side_effect=ValueError("bad")):
            rc, out = self._run(["--min", "5000"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "PROCEED:error:ValueError")


if __name__ == "__main__":
    unittest.main()
