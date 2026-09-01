"""The gate: an Authorization header plus a deployment's `[access]`
policy resolve to a principal or to one uniform refusal."""
from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llmwiki_service import auth, tokens

OPEN_READS = {"read": "open", "write": "token", "admin": "token"}
CLOSED = {"read": "token", "write": "token", "admin": "token"}
PEPPERS = tokens.Peppers({1: b"pepper-one"})


def bearer(token: str) -> str:
    return f"Bearer {token}"


class GateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "tokens.sqlite"
        self.store = tokens.TokenStore(self.path, PEPPERS)
        self.limiter = auth.FailureLimiter()
        self.reader = self.store.mint("reader-key", "reader")
        self.writer = self.store.mint("writer-key", "writer")
        self.admin = self.store.mint("admin-key", "admin")

    def gate(self, header, operation, access=CLOSED, client="peer"):
        return auth.authorize(
            header, operation, access, self.store, self.limiter, client
        )

    def test_open_read_needs_no_token(self) -> None:
        self.assertIs(self.gate(None, "read", OPEN_READS), auth.ANONYMOUS)

    def test_closed_read_needs_a_token(self) -> None:
        self.assertIs(self.gate(None, "read", CLOSED), auth.UNAUTHORIZED)

    def test_a_write_needs_a_token_even_when_the_policy_says_open(self) -> None:
        wide = {"read": "open", "write": "open", "admin": "open"}
        self.assertIs(self.gate(None, "write", wide), auth.UNAUTHORIZED)
        self.assertIs(self.gate(None, "admin", wide), auth.UNAUTHORIZED)

    def test_an_unrecognised_access_value_is_closed(self) -> None:
        self.assertIs(
            self.gate(None, "read", {"read": "opn"}), auth.UNAUTHORIZED
        )
        self.assertIs(self.gate(None, "read", {}), auth.UNAUTHORIZED)

    def test_an_operation_no_policy_names_is_closed(self) -> None:
        self.assertIs(
            self.gate(bearer(self.admin), "delete", OPEN_READS),
            auth.UNAUTHORIZED,
        )

    def test_a_reader_reads_and_nothing_else(self) -> None:
        principal = self.gate(bearer(self.reader), "read")
        self.assertEqual((principal.label, principal.role), ("reader-key", "reader"))
        self.assertIs(self.gate(bearer(self.reader), "write"), auth.UNAUTHORIZED)
        self.assertIs(self.gate(bearer(self.reader), "admin"), auth.UNAUTHORIZED)

    def test_a_writer_also_reads(self) -> None:
        self.assertEqual(self.gate(bearer(self.writer), "read").role, "writer")
        self.assertEqual(self.gate(bearer(self.writer), "write").role, "writer")
        self.assertIs(self.gate(bearer(self.writer), "admin"), auth.UNAUTHORIZED)

    def test_admin_is_a_superset(self) -> None:
        for operation in ("read", "write", "admin"):
            self.assertEqual(self.gate(bearer(self.admin), operation).role, "admin")

    def test_the_principal_carries_the_row_id_not_the_token(self) -> None:
        principal = self.gate(bearer(self.admin), "admin")
        self.assertEqual(principal.token_id, tokens.token_id(self.admin))
        self.assertNotIn(self.admin, repr(principal))

    def test_a_bad_token_is_refused_even_where_reads_are_open(self) -> None:
        self.assertIs(
            self.gate(bearer("llmwiki_nonsense"), "read", OPEN_READS),
            auth.UNAUTHORIZED,
        )


class UniformRefusalTest(unittest.TestCase):
    """Four different reasons, one indistinguishable answer."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "tokens.sqlite"
        self.store = tokens.TokenStore(self.path, PEPPERS)

    def refuse(self, token: str) -> object:
        limiter = auth.FailureLimiter()
        return auth.authorize(
            bearer(token), "read", CLOSED, self.store, limiter, "peer"
        )

    def test_every_failure_reason_gives_the_same_refusal(self) -> None:
        unknown_body = "llmwiki_" + "0" * 16 + "_" + "s" * 43
        unknown = unknown_body + tokens.checksum(unknown_body)

        live = self.store.mint("live", "reader")
        wrong_body = f"llmwiki_{tokens.token_id(live)}_" + "s" * 43
        wrong_secret = wrong_body + tokens.checksum(wrong_body)

        revoked = self.store.mint("revoked", "reader")
        self.store.revoke("revoked")

        expired = self.store.mint("expired", "reader")
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE tokens SET expires_at = ? WHERE label = ?",
                    ("2000-01-01T00:00:00Z", "expired"),
                )

        answers = [
            self.refuse(unknown),
            self.refuse(wrong_secret),
            self.refuse(revoked),
            self.refuse(expired),
        ]
        self.assertEqual(answers, [auth.UNAUTHORIZED] * 4)
        self.assertEqual({repr(answer) for answer in answers}, {repr(auth.UNAUTHORIZED)})

    def test_a_malformed_header_gives_that_same_refusal(self) -> None:
        limiter = auth.FailureLimiter()
        headers = [
            None,
            "",
            "   ",
            "Bearer",
            "Bearer ",
            "Basic abc123",
            "llmwiki_deadbeefdeadbeef_secret",
            "Bearer " + "\U0001f511" * 12,
            "Bearer llmwiki_" + "a" * 16 + "_café" * 9,
        ]
        for header in headers:
            with self.subTest(header=header):
                self.assertIs(
                    auth.authorize(
                        header, "read", CLOSED, self.store, limiter, "peer"
                    ),
                    auth.UNAUTHORIZED,
                )

    def test_the_bearer_scheme_is_case_insensitive(self) -> None:
        token = self.store.mint("laptop", "reader")
        limiter = auth.FailureLimiter()
        principal = auth.authorize(
            f"bearer {token}", "read", CLOSED, self.store, limiter, "peer"
        )
        self.assertEqual(principal.label, "laptop")


class RateLimitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = tokens.TokenStore(
            Path(self.dir.name) / "tokens.sqlite", PEPPERS
        )
        self.token = self.store.mint("laptop", "reader")
        self.limiter = auth.FailureLimiter()

    def gate(self, header, client="peer"):
        return auth.authorize(header, "read", CLOSED, self.store, self.limiter, client)

    def test_repeated_failures_are_rate_limited(self) -> None:
        for _ in range(auth.MAX_FAILURES_PER_WINDOW):
            self.assertIs(self.gate(bearer("rubbish")), auth.UNAUTHORIZED)
        self.assertIs(self.gate(bearer("rubbish")), auth.RATE_LIMITED)
        self.assertIs(self.gate(bearer(self.token)), auth.RATE_LIMITED)

    def test_one_caller_cannot_lock_out_another(self) -> None:
        for _ in range(auth.MAX_FAILURES_PER_WINDOW + 1):
            self.gate(bearer("rubbish"), client="noisy")
        self.assertEqual(self.gate(bearer(self.token), client="quiet").label, "laptop")

    def test_success_does_not_count_against_the_budget(self) -> None:
        for _ in range(auth.MAX_FAILURES_PER_WINDOW * 3):
            self.assertEqual(self.gate(bearer(self.token)).label, "laptop")

    def test_the_refusal_is_distinct_from_an_auth_failure(self) -> None:
        self.assertNotEqual(auth.RATE_LIMITED, auth.UNAUTHORIZED)


class FailureLimiterTest(unittest.TestCase):
    def test_the_budget_refills_after_the_window(self) -> None:
        limiter = auth.FailureLimiter(limit=2, window_sec=60)
        limiter.record_failure("peer", now=100.0)
        limiter.record_failure("peer", now=101.0)
        self.assertTrue(limiter.blocked("peer", now=102.0))
        self.assertFalse(limiter.blocked("peer", now=161.0))

    def test_an_unseen_caller_is_never_blocked(self) -> None:
        self.assertFalse(auth.FailureLimiter().blocked("stranger", now=0.0))

    def test_expired_callers_are_forgotten_once_the_table_is_full(self) -> None:
        limiter = auth.FailureLimiter(limit=2, window_sec=60)
        with mock.patch.object(auth, "MAX_TRACKED_CLIENTS", 3):
            for index in range(5):
                limiter.record_failure(f"peer-{index}", now=float(index))
            limiter.record_failure("late", now=1000.0)
        self.assertEqual(list(limiter.tracked()), ["late"])

    def test_the_table_never_exceeds_the_cap_when_nothing_expires(self) -> None:
        """Rotating client strings within one instant, none ever
        closing, is the flood that made the old sweep quadratic and
        left the cap bounding nothing. The table must hold at the cap
        regardless."""
        limiter = auth.FailureLimiter(limit=1, window_sec=60)
        with mock.patch.object(auth, "MAX_TRACKED_CLIENTS", 50):
            for index in range(500):
                limiter.record_failure(f"client-{index}", now=0.0)
            self.assertLessEqual(len(limiter.tracked()), 50)

    def test_a_flood_of_new_clients_cannot_reset_an_existing_block(self) -> None:
        """A caller already blocked must stay blocked once the table
        fills with other, unrelated clients. Plain LRU eviction would
        let a flood of cap-many strings buy the flooder's own reset."""
        limiter = auth.FailureLimiter(limit=1, window_sec=60)
        with mock.patch.object(auth, "MAX_TRACKED_CLIENTS", 20):
            limiter.record_failure("victim", now=0.0)
            self.assertTrue(limiter.blocked("victim", now=0.0))
            for index in range(200):
                limiter.record_failure(f"flood-{index}", now=0.0)
            self.assertLessEqual(len(limiter.tracked()), 20)
            self.assertTrue(limiter.blocked("victim", now=0.0))


class RequiredClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        path = Path(self.dir.name) / "tokens.sqlite"
        self.store = tokens.TokenStore(path, PEPPERS)
        self.limiter = auth.FailureLimiter()

    def test_authorize_requires_a_client(self) -> None:
        """A phase 2 integration that forgets to pass the peer address
        must fail loudly at the call site, not silently pool every
        caller into one rate-limit bucket."""
        with self.assertRaises(TypeError):
            auth.authorize(None, "read", OPEN_READS, self.store, self.limiter)


if __name__ == "__main__":
    unittest.main()
