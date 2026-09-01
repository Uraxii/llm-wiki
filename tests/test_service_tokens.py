"""The token format, the keyed hash, the pepper versions, and the
sqlite store behind mint, list, revoke, and verify."""
from __future__ import annotations

import contextlib
import hashlib
import io
import logging
import re
import sqlite3
import string
import tempfile
import unittest
from pathlib import Path

from llmwiki_service import tokens

PAST = "2000-01-01T00:00:00Z"
FUTURE = "2999-01-01T00:00:00Z"
URLSAFE = set(string.ascii_letters + string.digits + "-_")


def peppers(*pairs: tuple[int, bytes]) -> tokens.Peppers:
    return tokens.Peppers(dict(pairs))


ONE = ((1, b"pepper-one"),)
TWO = ((1, b"pepper-one"), (2, b"pepper-two"))


class Base62Test(unittest.TestCase):
    """Deterministic coverage of the padding branch. `checksum` exercises
    it too, but only through a randomly generated token, so whether it
    hits a short encoding is a coin flip; a chosen input pins it down."""

    def test_a_short_encoding_is_zero_padded_on_the_left(self) -> None:
        self.assertEqual(tokens._base62(0), "000000")
        self.assertEqual(tokens._base62(1), "000001")

    def test_padding_never_defaults_to_a_space(self) -> None:
        self.assertNotIn(" ", tokens._base62(0))


class TokenFormatTest(unittest.TestCase):
    def test_shape_is_prefix_id_secret_checksum(self) -> None:
        row_id, token = tokens.new_token()
        self.assertTrue(token.startswith("llmwiki_"))
        prefix, ident, rest = token.split("_", 2)
        self.assertEqual(prefix, "llmwiki")
        self.assertEqual(ident, row_id)
        self.assertRegex(ident, r"\A[0-9a-f]{16}\Z")
        secret, crc = rest[:-6], rest[-6:]
        self.assertEqual(len(secret), 43)
        self.assertLessEqual(set(secret), URLSAFE)
        self.assertEqual(len(crc), 6)
        self.assertLessEqual(set(crc), set(tokens.BASE62))

    def test_checksum_covers_everything_before_it(self) -> None:
        _row_id, token = tokens.new_token()
        self.assertEqual(token[-6:], tokens.checksum(token[:-6]))

    def test_two_tokens_never_repeat(self) -> None:
        minted = {tokens.new_token()[1] for _ in range(50)}
        self.assertEqual(len(minted), 50)

    def test_well_formed_token_yields_its_id(self) -> None:
        row_id, token = tokens.new_token()
        self.assertEqual(tokens.token_id(token), row_id)

    def test_altered_secret_fails_its_checksum(self) -> None:
        """The altered character sits in the secret, so the token stays
        structurally well formed and only the checksum can reject it."""
        _row_id, token = tokens.new_token()
        cut = len("llmwiki_") + 16 + 1 + 5
        flipped = "A" if token[cut] != "A" else "B"
        broken = token[:cut] + flipped + token[cut + 1 :]
        self.assertEqual(broken.split("_", 2)[1], token.split("_", 2)[1])
        self.assertIsNone(tokens.token_id(broken))

    def test_truncated_token_rejected(self) -> None:
        _row_id, token = tokens.new_token()
        self.assertIsNone(tokens.token_id(token[:-1]))
        self.assertIsNone(tokens.token_id(token[:12]))
        self.assertIsNone(tokens.token_id(""))
        self.assertIsNone(tokens.token_id("llmwiki"))

    def test_foreign_prefix_rejected(self) -> None:
        body = "someother_" + "a" * 16 + "_" + "s" * 43
        self.assertIsNone(tokens.token_id(body + tokens.checksum(body)))

    def test_non_hex_id_rejected(self) -> None:
        body = "llmwiki_" + "z" * 16 + "_" + "s" * 43
        self.assertIsNone(tokens.token_id(body + tokens.checksum(body)))

    def test_a_non_ascii_token_is_refused_and_never_raises(self) -> None:
        """`secrets.compare_digest` raises TypeError on non-ASCII text.
        A junk token has to read as a refusal, not as a crash, or the
        refusal stops being uniform."""
        for junk in ("\U0001f511" * 10, "llmwiki_" + "a" * 16 + "_café" * 9):
            with self.subTest(junk=junk):
                self.assertIsNone(tokens.token_id(junk))

    def test_empty_secret_rejected(self) -> None:
        body = "llmwiki_" + "a" * 16 + "_"
        self.assertIsNone(tokens.token_id(body + tokens.checksum(body)))

    def test_a_token_that_is_only_a_checksum_is_rejected(self) -> None:
        """`checksum("")` is `"000000"`, so a bare 6 zero token is the
        edge case the length guard exists to catch."""
        self.assertIsNone(tokens.token_id("000000"))


class HashTest(unittest.TestCase):
    def test_hash_is_keyed_blake2b_at_32_bytes(self) -> None:
        _row_id, token = tokens.new_token()
        expected = hashlib.blake2b(
            token.encode(), key=b"pepper-one", digest_size=32
        ).hexdigest()
        self.assertEqual(tokens.token_hash(token, b"pepper-one"), expected)
        self.assertEqual(len(expected), 64)

    def test_a_different_pepper_gives_a_different_hash(self) -> None:
        _row_id, token = tokens.new_token()
        self.assertNotEqual(
            tokens.token_hash(token, b"pepper-one"),
            tokens.token_hash(token, b"pepper-two"),
        )


class PeppersTest(unittest.TestCase):
    def test_highest_version_is_current(self) -> None:
        self.assertEqual(peppers(*TWO).current, 2)

    def test_no_pepper_is_not_a_representable_state(self) -> None:
        with self.assertRaises(ValueError):
            tokens.Peppers({})

    def test_repr_hides_the_secrets(self) -> None:
        text = repr(peppers(*TWO))
        self.assertNotIn("pepper-one", text)
        self.assertNotIn("pepper-two", text)
        self.assertIn("1", text)

    def test_env_parses_versioned_entries(self) -> None:
        parsed = tokens.peppers_from_env(
            {tokens.PEPPER_ENV_VAR: "2:second 1:first"}
        )
        self.assertEqual(parsed.keys, {1: b"first", 2: b"second"})
        self.assertEqual(parsed.current, 2)

    def test_env_unset_refused(self) -> None:
        for value in ({}, {tokens.PEPPER_ENV_VAR: "   "}):
            with self.assertRaises(ValueError):
                tokens.peppers_from_env(value)

    def test_a_missing_variable_is_treated_as_empty_not_as_one_entry(self) -> None:
        with self.assertRaises(ValueError) as caught:
            tokens.peppers_from_env({})
        self.assertIn("unset or empty", str(caught.exception))

    def test_a_secret_containing_a_colon_is_still_one_entry(self) -> None:
        """`entry.partition(":")` must split on the first colon, so a
        secret is free to contain one."""
        parsed = tokens.peppers_from_env({tokens.PEPPER_ENV_VAR: "1:sec:ret"})
        self.assertEqual(parsed.keys, {1: b"sec:ret"})

    def test_env_malformed_refused_without_quoting_the_secret(self) -> None:
        with self.assertRaises(ValueError) as caught:
            tokens.peppers_from_env({tokens.PEPPER_ENV_VAR: "notaversion"})
        self.assertNotIn("notaversion", str(caught.exception))

    def test_env_superscript_version_refused_without_quoting_the_value(self) -> None:
        # "²".isdigit() is True but int("²") raises, so a
        # version built from a superscript digit must be rejected by
        # this module's own check, not smuggled into int() and crash
        # there with the raw entry in the message.
        with self.assertRaises(ValueError) as caught:
            tokens.peppers_from_env({tokens.PEPPER_ENV_VAR: "²:secret"})
        message = str(caught.exception)
        self.assertIn(tokens.PEPPER_ENV_VAR, message)
        self.assertNotIn("²", message)

    def test_env_duplicate_version_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            tokens.peppers_from_env({tokens.PEPPER_ENV_VAR: "1:a 1:b"})
        self.assertIn("duplicate", str(caught.exception))

    def test_an_oversized_pepper_is_not_a_representable_state(self) -> None:
        with self.assertRaises(ValueError):
            tokens.Peppers({1: b"x" * 65})

    def test_env_oversized_pepper_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            tokens.peppers_from_env({tokens.PEPPER_ENV_VAR: "1:" + "x" * 65})
        self.assertNotIn("x" * 65, str(caught.exception))


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "tokens.sqlite"
        self.store = tokens.TokenStore(self.path, peppers(*ONE))

    def rows(self) -> list[tuple]:
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(
                "SELECT id, token_hash, pepper_version, label, role, "
                "created_at, last_used_at, expires_at, revoked_at FROM tokens"
            ).fetchall()

    def test_table_has_exactly_the_nine_columns(self) -> None:
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            info = conn.execute("PRAGMA table_info(tokens)").fetchall()
        self.assertEqual(
            [column[1] for column in info],
            [
                "id",
                "token_hash",
                "pepper_version",
                "label",
                "role",
                "created_at",
                "last_used_at",
                "expires_at",
                "revoked_at",
            ],
        )
        self.assertEqual([column[1] for column in info if column[5]], ["id"])

    def test_label_is_unique(self) -> None:
        self.store.mint("laptop", "reader")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.mint("laptop", "writer")

    def test_mint_returns_a_verifying_token(self) -> None:
        token = self.store.mint("laptop", "writer")
        row = self.store.verify(token)
        self.assertIsNotNone(row)
        self.assertEqual((row.label, row.role), ("laptop", "writer"))

    def test_verify_returns_the_original_created_at(self) -> None:
        token = self.store.mint("laptop", "reader")
        created = self.store.list_tokens()[0].created_at
        row = self.store.verify(token)
        self.assertEqual(row.created_at, created)

    def test_verify_returns_the_freshly_stamped_last_used_at(self) -> None:
        token = self.store.mint("laptop", "reader")
        row = self.store.verify(token, now="2026-09-01T10:00:00Z")
        self.assertEqual(row.last_used_at, "2026-09-01T10:00:00Z")

    def test_mint_rejects_an_unknown_role(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.store.mint("laptop", "superuser")
        self.assertIn("superuser", str(caught.exception))

    def test_mint_rejects_an_empty_label(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.store.mint("", "reader")
        self.assertEqual(
            str(caught.exception), "a token needs a label to be revoked by"
        )

    def test_mint_rejects_a_label_that_could_forge_a_log_line(self) -> None:
        for label in ("two\nlines", "tab\there", "bell\a"):
            with self.subTest(label=label):
                with self.assertRaises(ValueError) as caught:
                    self.store.mint(label, "reader")
                self.assertEqual(
                    str(caught.exception),
                    "a label must hold no control characters",
                )

    def test_a_non_ascii_token_verifies_to_none(self) -> None:
        self.assertIsNone(self.store.verify("\U0001f600" * 12))

    def test_store_keeps_no_plaintext(self) -> None:
        token = self.store.mint("laptop", "reader")
        self.assertNotIn(token, self.path.read_bytes().decode("latin-1"))

    def test_list_shows_label_role_and_last_used_only(self) -> None:
        token = self.store.mint("laptop", "reader")
        self.store.verify(token)
        (row,) = self.store.list_tokens()
        self.assertEqual((row.label, row.role), ("laptop", "reader"))
        self.assertIsNotNone(row.last_used_at)
        self.assertNotIn("hash", vars(row))
        rendered = repr(row)
        self.assertNotIn(token, rendered)
        self.assertNotIn(self.rows()[0][1], rendered)

    def test_last_used_at_starts_null_and_updates_on_use(self) -> None:
        token = self.store.mint("laptop", "reader")
        self.assertIsNone(self.rows()[0][6])
        self.store.verify(token, now="2026-09-01T10:00:00Z")
        self.assertEqual(self.rows()[0][6], "2026-09-01T10:00:00Z")
        self.store.verify(token, now="2026-09-01T11:00:00Z")
        self.assertEqual(self.rows()[0][6], "2026-09-01T11:00:00Z")

    def test_last_used_at_does_not_move_on_a_failed_verify(self) -> None:
        token = self.store.mint("laptop", "reader")
        self.store.verify(token, now="2026-09-01T10:00:00Z")
        body = f"llmwiki_{tokens.token_id(token)}_" + "s" * 43
        wrong = body + tokens.checksum(body)
        self.assertIsNone(self.store.verify(wrong, now="2026-09-01T12:00:00Z"))
        self.assertEqual(self.rows()[0][6], "2026-09-01T10:00:00Z")

    def test_unknown_id_with_a_valid_checksum_refused(self) -> None:
        body = "llmwiki_" + "0" * 16 + "_" + "s" * 43
        self.assertIsNone(self.store.verify(body + tokens.checksum(body)))

    def test_wrong_secret_for_a_known_id_refused(self) -> None:
        row_id = tokens.token_id(self.store.mint("laptop", "reader"))
        body = f"llmwiki_{row_id}_" + "s" * 43
        self.assertIsNone(self.store.verify(body + tokens.checksum(body)))

    def test_bad_checksum_never_reaches_the_database(self) -> None:
        """A structurally perfect token with the wrong checksum. Only
        the checksum stands between it and the lookup path."""
        token = self.store.mint("laptop", "reader")
        body = token[:-6]
        wrong_crc = "".join("A" if c != "A" else "B" for c in token[-6:])
        self.assertNotEqual(wrong_crc, tokens.checksum(body))

        def boom() -> sqlite3.Connection:
            raise AssertionError("verify opened the database")

        self.store._connect = boom
        self.assertIsNone(self.store.verify(body + wrong_crc))

    def test_revoke_takes_a_label(self) -> None:
        token = self.store.mint("laptop", "reader")
        self.assertTrue(self.store.revoke("laptop"))
        self.assertIsNone(self.store.verify(token))
        self.assertIsNotNone(self.rows()[0][8])

    def test_revoke_is_idempotent_and_reports_unknown_labels(self) -> None:
        self.store.mint("laptop", "reader")
        self.assertTrue(self.store.revoke("laptop"))
        self.assertFalse(self.store.revoke("laptop"))
        self.assertFalse(self.store.revoke("never-minted"))

    def test_expired_row_refused(self) -> None:
        token = self.store.mint("laptop", "reader")
        self.set_expiry(PAST)
        self.assertIsNone(self.store.verify(token))

    def test_future_expiry_still_verifies(self) -> None:
        token = self.store.mint("laptop", "reader")
        self.set_expiry(FUTURE)
        self.assertIsNotNone(self.store.verify(token))

    def test_policy_leaves_expiry_null(self) -> None:
        self.store.mint("laptop", "reader")
        self.assertIsNone(self.rows()[0][7])

    def set_expiry(self, value: str) -> None:
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            with conn:
                conn.execute("UPDATE tokens SET expires_at = ?", (value,))


class PepperRotationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "tokens.sqlite"

    def stored(self) -> tuple:
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(
                "SELECT token_hash, pepper_version FROM tokens"
            ).fetchone()

    def test_token_minted_under_version_one_survives_rotation(self) -> None:
        token = tokens.TokenStore(self.path, peppers(*ONE)).mint("laptop", "reader")
        self.assertEqual(self.stored()[1], 1)

        rotated = tokens.TokenStore(self.path, peppers(*TWO))
        self.assertIsNotNone(rotated.verify(token))

        digest, version = self.stored()
        self.assertEqual(version, 2)
        self.assertEqual(digest, tokens.token_hash(token, b"pepper-two"))

    def test_after_rotation_the_old_pepper_can_be_dropped(self) -> None:
        token = tokens.TokenStore(self.path, peppers(*ONE)).mint("laptop", "reader")
        tokens.TokenStore(self.path, peppers(*TWO)).verify(token)
        dropped = tokens.TokenStore(self.path, peppers((2, b"pepper-two")))
        self.assertIsNotNone(dropped.verify(token))

    def test_a_pepper_version_not_configured_refuses(self) -> None:
        token = tokens.TokenStore(self.path, peppers(*ONE)).mint("laptop", "reader")
        other = tokens.TokenStore(self.path, peppers((2, b"pepper-two")))
        self.assertIsNone(other.verify(token))

    def test_the_wrong_pepper_at_the_same_version_refuses(self) -> None:
        token = tokens.TokenStore(self.path, peppers(*ONE)).mint("laptop", "reader")
        other = tokens.TokenStore(self.path, peppers((1, b"not-the-pepper")))
        self.assertIsNone(other.verify(token))


class DisclosureTest(unittest.TestCase):
    def test_a_mint_writes_the_secret_nowhere_but_its_return_value(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "tokens.sqlite"
        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        root = logging.getLogger()
        root.addHandler(handler)
        previous = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, previous)
        self.addCleanup(root.removeHandler, handler)

        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            store = tokens.TokenStore(path, peppers(*ONE))
            token = store.mint("laptop", "admin")
            store.verify(token)
            print(repr(store), repr(store.peppers), repr(store.list_tokens()))

        output = captured.getvalue()
        self.assertNotIn(token, output)
        self.assertNotIn(token.split("_", 2)[2], output)
        self.assertNotIn("pepper-one", output)

    def test_a_duplicate_label_error_names_no_secret(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = tokens.TokenStore(
            Path(directory.name) / "tokens.sqlite", peppers(*ONE)
        )
        token = store.mint("laptop", "reader")
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            store.mint("laptop", "reader")
        self.assertNotIn(token, str(caught.exception))

    def test_no_module_source_line_prints_a_token(self) -> None:
        source = Path(tokens.__file__).read_text(encoding="utf-8")
        self.assertNotRegex(source, re.compile(r"^\s*print\(", re.MULTILINE))


if __name__ == "__main__":
    unittest.main()
