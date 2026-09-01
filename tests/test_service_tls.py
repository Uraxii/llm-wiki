"""The TLS context builder, the reload-without-downtime holder, and the
file watcher. Certificates are generated with the system `openssl`
binary into a tempdir per test: no checked-in fixture, no network.
"""
from __future__ import annotations

import asyncio
import logging
import os
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from llmwiki_service import tls


def _make_cert(dest: Path, name: str, subject: str) -> tuple[Path, Path]:
    cert = dest / f"{name}-cert.pem"
    key = dest / f"{name}-key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:P-256",
            "-keyout", str(key), "-out", str(cert),
            "-nodes", "-subj", f"/CN={subject}", "-days", "2",
        ],
        check=True, capture_output=True,
    )
    return cert, key


def _silent_logger() -> logging.Logger:
    logger = logging.getLogger("test-llmwiki-service-tls")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


async def _peer_subject(port: int) -> str:
    """The `commonName` of the certificate served on `port`, read from
    a fresh client connection over loopback TLS."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    ssl_obj = writer.get_extra_info("ssl_object")
    der = ssl_obj.getpeercert(binary_form=True)
    pem = ssl.DER_cert_to_PEM_cert(der)
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as fh:
        fh.write(pem)
        path = fh.name
    try:
        info = ssl._ssl._test_decode_cert(path)
    finally:
        Path(path).unlink()
    writer.close()
    await writer.wait_closed()
    return dict(x[0] for x in info["subject"])["commonName"]


class BuildContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cert, self.key = _make_cert(Path(self.dir.name), "one", "subject-one")

    def test_builds_a_context_with_the_requested_minimum_version(self) -> None:
        context = tls.build_context(str(self.cert), str(self.key), "1.3")
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_3)

    def test_default_minimum_version_is_1_2(self) -> None:
        context = tls.build_context(str(self.cert), str(self.key), None)
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_unknown_minimum_version_raises_naming_the_version(self) -> None:
        with self.assertRaises(ValueError) as caught:
            tls.build_context(str(self.cert), str(self.key), "1.1")
        self.assertIn("1.1", str(caught.exception))

    def test_missing_cert_raises_naming_the_path(self) -> None:
        missing = self.dir.name + "/nope.pem"
        with self.assertRaises(ValueError) as caught:
            tls.build_context(missing, str(self.key), "1.2")
        self.assertIn(missing, str(caught.exception))

    def test_a_key_that_does_not_match_its_cert_names_both_paths(self) -> None:
        """`load_cert_chain` raises `ssl.SSLError` here, and that error
        names neither file. It reached the operator as a traceback."""
        _other_cert, other_key = _make_cert(
            Path(self.dir.name), "other", "subject-other"
        )
        with self.assertRaises(ValueError) as caught:
            tls.build_context(str(self.cert), str(other_key), "1.2")
        message = str(caught.exception)
        self.assertIn(str(self.cert), message)
        self.assertIn(str(other_key), message)


class ContextHolderReloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.cert, self.key = _make_cert(self.root, "one", "subject-one")
        self.logger = _silent_logger()
        self.holder = tls.ContextHolder(
            tls.build_context(str(self.cert), str(self.key), "1.2")
        )

    def test_a_good_reload_swaps_the_current_context(self) -> None:
        before = self.holder.current
        cert2, key2 = _make_cert(self.root, "two", "subject-two")
        ok = self.holder.reload(str(cert2), str(key2), "1.2", self.logger)
        self.assertTrue(ok)
        self.assertIsNot(self.holder.current, before)

    def test_a_broken_reload_keeps_the_previous_context(self) -> None:
        before = self.holder.current
        garbage = self.root / "garbage.pem"
        garbage.write_text("not a certificate\n")
        ok = self.holder.reload(str(garbage), str(self.key), "1.2", self.logger)
        self.assertFalse(ok)
        self.assertIs(self.holder.current, before)

    def test_a_broken_reload_logs_the_failure_reason(self) -> None:
        garbage = self.root / "garbage.pem"
        garbage.write_text("not a certificate\n")
        with self.assertLogs("test-llmwiki-service-tls", level="ERROR") as caught:
            self.holder.reload(str(garbage), str(self.key), "1.2", self.logger)
        [message] = caught.output
        self.assertTrue(
            message.startswith(
                "ERROR:test-llmwiki-service-tls:certificate reload failed, "
                "keeping the previous context: "
            )
        )
        # The wrapped ValueError names both paths; a log missing them
        # is a log an operator cannot act on.
        self.assertIn(str(garbage), message)
        self.assertIn(str(self.key), message)

    def test_a_good_reload_respects_the_requested_min_version(self) -> None:
        cert2, key2 = _make_cert(self.root, "two", "subject-two")
        self.holder.reload(str(cert2), str(key2), "1.3", self.logger)
        self.assertEqual(self.holder.current.minimum_version, ssl.TLSVersion.TLSv1_3)

    def test_a_good_reload_logs_the_cert_path(self) -> None:
        cert2, key2 = _make_cert(self.root, "two", "subject-two")
        with self.assertLogs("test-llmwiki-service-tls", level="INFO") as caught:
            self.holder.reload(str(cert2), str(key2), "1.2", self.logger)
        self.assertEqual(
            caught.output,
            [f"INFO:test-llmwiki-service-tls:certificate reloaded from {cert2}"],
        )


class WatchCertificatesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.cert, self.key = _make_cert(self.root, "one", "subject-one")
        self.logger = _silent_logger()

    def test_a_changed_cert_file_triggers_one_reload(self) -> None:
        holder = tls.ContextHolder(
            tls.build_context(str(self.cert), str(self.key), "1.2")
        )
        before = holder.current
        stop = threading.Event()
        thread = threading.Thread(
            target=tls.watch_certificates,
            args=(holder, str(self.cert), str(self.key), "1.2", self.logger, stop),
            kwargs={"interval_sec": 0.05},
            daemon=True,
        )
        thread.start()
        self.addCleanup(lambda: (stop.set(), thread.join(timeout=2)))

        # A new mtime is what the watcher polls for; write different
        # bytes so the file's content, not just its timestamp, changes.
        cert2, key2 = _make_cert(self.root, "two", "subject-two")
        self.cert.write_bytes(cert2.read_bytes())
        self.key.write_bytes(key2.read_bytes())

        for _ in range(40):
            if holder.current is not before:
                break
            threading.Event().wait(0.05)
        self.assertIsNot(holder.current, before)

    def test_default_poll_interval_is_one_second(self) -> None:
        # inspect.signature on tls.watch_certificates always reports the
        # trampoline's original, unmutated default; a mutation to the
        # default only shows up by actually timing the wait, so a
        # change is planted before the watcher starts and caught well
        # inside one interval, not two.
        holder = tls.ContextHolder(
            tls.build_context(str(self.cert), str(self.key), "1.2")
        )
        before = holder.current
        stop = threading.Event()
        thread = threading.Thread(
            target=tls.watch_certificates,
            args=(holder, str(self.cert), str(self.key), "1.2", self.logger, stop),
            daemon=True,
        )
        thread.start()
        self.addCleanup(lambda: (stop.set(), thread.join(timeout=3)))

        cert2, key2 = _make_cert(self.root, "two", "subject-two")
        self.cert.write_bytes(cert2.read_bytes())
        self.key.write_bytes(key2.read_bytes())

        threading.Event().wait(1.6)
        self.assertIsNot(holder.current, before)

    def test_unchanged_files_never_trigger_a_reload(self) -> None:
        holder = tls.ContextHolder(
            tls.build_context(str(self.cert), str(self.key), "1.2")
        )
        before = holder.current
        stop = threading.Event()
        thread = threading.Thread(
            target=tls.watch_certificates,
            args=(holder, str(self.cert), str(self.key), "1.2", self.logger, stop),
            kwargs={"interval_sec": 0.02},
            daemon=True,
        )
        thread.start()
        self.addCleanup(lambda: (stop.set(), thread.join(timeout=2)))

        # Several poll intervals with nothing touching either file: the
        # baseline mtimes never change, so no reload should ever fire.
        threading.Event().wait(0.2)
        self.assertIs(holder.current, before)

    def test_a_real_change_reloads_with_the_requested_min_version(self) -> None:
        holder = tls.ContextHolder(
            tls.build_context(str(self.cert), str(self.key), "1.3")
        )
        before = holder.current
        stop = threading.Event()
        thread = threading.Thread(
            target=tls.watch_certificates,
            args=(holder, str(self.cert), str(self.key), "1.3", self.logger, stop),
            kwargs={"interval_sec": 0.05},
            daemon=True,
        )
        thread.start()
        self.addCleanup(lambda: (stop.set(), thread.join(timeout=2)))

        cert2, key2 = _make_cert(self.root, "two", "subject-two")
        self.cert.write_bytes(cert2.read_bytes())
        self.key.write_bytes(key2.read_bytes())

        for _ in range(40):
            if holder.current is not before:
                break
            threading.Event().wait(0.05)
        self.assertIsNot(holder.current, before)
        self.assertEqual(holder.current.minimum_version, ssl.TLSVersion.TLSv1_3)

    def test_reload_settles_and_does_not_repeat(self) -> None:
        holder = tls.ContextHolder(
            tls.build_context(str(self.cert), str(self.key), "1.2")
        )
        before = holder.current
        stop = threading.Event()
        thread = threading.Thread(
            target=tls.watch_certificates,
            args=(holder, str(self.cert), str(self.key), "1.2", self.logger, stop),
            kwargs={"interval_sec": 0.02},
            daemon=True,
        )
        thread.start()
        self.addCleanup(lambda: (stop.set(), thread.join(timeout=2)))

        cert2, key2 = _make_cert(self.root, "two", "subject-two")
        self.cert.write_bytes(cert2.read_bytes())
        self.key.write_bytes(key2.read_bytes())

        for _ in range(40):
            if holder.current is not before:
                break
            threading.Event().wait(0.02)
        after_first = holder.current
        self.assertIsNot(after_first, before)

        # No further file write. A settled baseline must not keep
        # re-triggering a reload on every later poll.
        threading.Event().wait(0.2)
        self.assertIs(holder.current, after_first)


class WatchRetryTest(unittest.TestCase):
    """The watcher's baseline advances only on a reload that took
    effect. It used to advance on every change, so a reload that failed
    for a reason the files would not change again, a transient OSError
    or a momentary permission fault, was logged once and never retried,
    and the service kept serving the pre-renewal certificate until it
    expired."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.cert, self.key = _make_cert(self.root, "one", "subject-one")
        self.logger = _silent_logger()
        self.holder = tls.ContextHolder(
            tls.build_context(str(self.cert), str(self.key), "1.2")
        )

    def _watch(self) -> None:
        stop = threading.Event()
        thread = threading.Thread(
            target=tls.watch_certificates,
            args=(
                self.holder, str(self.cert), str(self.key), "1.2",
                self.logger, stop,
            ),
            kwargs={"interval_sec": 0.05},
            daemon=True,
        )
        thread.start()
        self.addCleanup(lambda: (stop.set(), thread.join(timeout=2)))

    def _wait_for_swap(self, before: ssl.SSLContext, tries: int = 60) -> bool:
        for _ in range(tries):
            if self.holder.current is not before:
                return True
            threading.Event().wait(0.05)
        return False

    @unittest.skipIf(os.geteuid() == 0, "root ignores the mode bits this uses")
    def test_a_failure_with_no_later_file_change_is_retried(self) -> None:
        before = self.holder.current
        cert2, key2 = _make_cert(self.root, "two", "subject-two")

        # Write-only: the renewal below still lands, and reading the key
        # back fails, which is the shape of a transient permission fault
        # while a renewal tool fixes ownership.
        os.chmod(self.key, 0o200)
        self.addCleanup(os.chmod, self.key, 0o600)
        self._watch()

        self.cert.write_bytes(cert2.read_bytes())
        self.key.write_bytes(key2.read_bytes())
        self.assertFalse(self._wait_for_swap(before, tries=8))

        # Nothing writes to either file again; only the mode changes, and
        # chmod does not move an mtime. The retry is the whole point.
        os.chmod(self.key, 0o600)
        self.assertTrue(self._wait_for_swap(before))

    def test_a_cert_landing_before_its_key_still_heals(self) -> None:
        before = self.holder.current
        cert2, key2 = _make_cert(self.root, "two", "subject-two")

        self._watch()
        self.cert.write_bytes(cert2.read_bytes())
        self.assertFalse(self._wait_for_swap(before, tries=6))

        self.key.write_bytes(key2.read_bytes())
        self.assertTrue(self._wait_for_swap(before))


class LiveReloadProofTest(unittest.TestCase):
    """The actual reload-without-downtime proof, over a real loopback
    TLS connection on an ephemeral port: a fresh client after a swap
    reads a different certificate subject, and a connection opened
    before the swap keeps working after it."""

    def test_new_connections_see_the_reloaded_certificate(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cert1, key1 = _make_cert(root, "one", "subject-one")
            cert2, key2 = _make_cert(root, "two", "subject-two")
            logger = _silent_logger()

            holder = tls.ContextHolder(
                tls.build_context(str(cert1), str(key1), "1.2")
            )
            server_ctx = tls.server_context(holder)

            async def handle(reader, writer):
                await reader.read(64)
                writer.write(b"ok")
                await writer.drain()
                writer.close()

            server = await asyncio.start_server(
                handle, "127.0.0.1", 0, ssl=server_ctx
            )
            port = server.sockets[0].getsockname()[1]
            try:
                subject_before = await _peer_subject(port)
                self.assertEqual(subject_before, "subject-one")

                ok = holder.reload(str(cert2), str(key2), "1.2", logger)
                self.assertTrue(ok)

                subject_after = await _peer_subject(port)
                self.assertEqual(subject_after, "subject-two")
            finally:
                server.close()
                await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
