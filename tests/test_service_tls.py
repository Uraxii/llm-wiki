"""The TLS context builder, the reload-without-downtime holder, and the
file watcher. Certificates are generated with the system `openssl`
binary into a tempdir per test: no checked-in fixture, no network.
"""
from __future__ import annotations

import asyncio
import logging
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

    def test_unknown_minimum_version_raises(self) -> None:
        with self.assertRaises(ValueError):
            tls.build_context(str(self.cert), str(self.key), "1.1")

    def test_missing_cert_raises(self) -> None:
        with self.assertRaises((ssl.SSLError, OSError)):
            tls.build_context(str(self.dir.name + "/nope.pem"), str(self.key), "1.2")


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

    def test_a_broken_reload_logs_loudly(self) -> None:
        garbage = self.root / "garbage.pem"
        garbage.write_text("not a certificate\n")
        with self.assertLogs("test-llmwiki-service-tls", level="ERROR") as caught:
            self.holder.reload(str(garbage), str(self.key), "1.2", self.logger)
        self.assertTrue(any("reload failed" in line for line in caught.output))


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
