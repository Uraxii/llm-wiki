"""`_parse_bind`, the argument-refusal path of `main`, and one real
subprocess run of `python -m llmwiki_service` proving the startup
order: refusals block the bind, and a clean deployment serves
`/health` over TLS on an ephemeral port. No fixed port, no network
beyond loopback.
"""
from __future__ import annotations

import contextlib
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from llmwiki_service import __main__ as service_main

PYTHON = sys.executable
PEPPER_ENV = {
    "LLM_WIKI_PEPPER": "1:pepper-one",
    "LLM_WIKI_BOOTSTRAP_ADMIN_TOKEN": "bootstrap-secret",
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _make_cert(dest: Path) -> tuple[Path, Path]:
    cert, key = dest / "cert.pem", dest / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:P-256",
            "-keyout", str(key), "-out", str(cert),
            "-nodes", "-subj", "/CN=test", "-days", "2",
        ],
        check=True, capture_output=True,
    )
    return cert, key


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class ParseBindTest(unittest.TestCase):
    def test_host_and_port(self) -> None:
        self.assertEqual(
            service_main._parse_bind("127.0.0.1:8443"), ("127.0.0.1", 8443)
        )

    def test_ipv6_brackets_are_stripped(self) -> None:
        self.assertEqual(service_main._parse_bind("[::]:8443"), ("::", 8443))

    def test_no_colon_raises(self) -> None:
        with self.assertRaises(ValueError):
            service_main._parse_bind("no-port-here")


class MainArgumentRefusalTest(unittest.TestCase):
    """Hermetic: these never reach a socket, so they need no subprocess."""

    def test_no_argument_exits_nonzero(self) -> None:
        self.assertEqual(service_main.main(["llmwiki_service"]), 1)

    def test_missing_path_exits_nonzero(self) -> None:
        self.assertEqual(
            service_main.main(["llmwiki_service", "/no/such/deploy.toml"]), 1
        )


class RunningServiceTest(unittest.TestCase):
    """A real `python -m llmwiki_service` subprocess, bound to an
    ephemeral loopback port, per the phase's rule that a TestClient
    does not stand in for a running service."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.kb = self.root / "kb"
        self.kb.mkdir()
        self.cert, self.key = _make_cert(self.root)

    def _write(self, port: int, **tls_overrides: str) -> Path:
        path = self.root / "deploy.toml"
        path.write_text(
            f"""
[server]
bind = "127.0.0.1:{port}"
state = "{self.state}"

[tls]
mode = "terminate"
cert = "{self.cert}"
key = "{self.key}"

[access]
read = "open"
write = "token"
admin = "token"

[kbs.demo]
path = "{self.kb}"
"""
        )
        return path

    def test_a_clean_deployment_serves_health_over_tls(self) -> None:
        port = _free_port()
        deploy = self._write(port)
        proc = subprocess.Popen(
            [PYTHON, "-m", "llmwiki_service", str(deploy)],
            cwd=Path(__file__).resolve().parent.parent,
            env=PEPPER_ENV,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            self._wait_for_port(port, proc)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
                with ctx.wrap_socket(raw, server_hostname="test") as tls_sock:
                    tls_sock.send(
                        b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
                    )
                    reply = b""
                    while chunk := tls_sock.recv(200):
                        reply += chunk
            self.assertIn(b"200", reply)
            self.assertIn(b"ok", reply)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            proc.stdout.close()

    def test_a_startup_refusal_binds_nothing(self) -> None:
        port = _free_port()
        deploy = self._write(port)
        # Row 6: [access] write = "open" is always refused.
        deploy.write_text(deploy.read_text().replace(
            'write = "token"', 'write = "open"'
        ))
        proc = subprocess.run(
            [PYTHON, "-m", "llmwiki_service", str(deploy)],
            cwd=Path(__file__).resolve().parent.parent,
            env=PEPPER_ENV,
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("write", proc.stderr)
        self.assertFalse(_port_open(port))

    def _wait_for_port(self, port: int, proc: subprocess.Popen) -> None:
        for _ in range(50):
            if _port_open(port):
                return
            if proc.poll() is not None:
                self.fail(f"service exited early:\n{proc.stdout.read()}")
            time.sleep(0.1)
        self.fail("service never opened its port")


if __name__ == "__main__":
    unittest.main()
