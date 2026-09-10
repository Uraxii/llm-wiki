"""`_parse_bind`, the argument-refusal path of `main`, and one real
subprocess run of `python -m llmwiki_service` proving the startup
order: refusals block the bind, and a clean deployment serves
`/health` over TLS on an ephemeral port. No fixed port, no network
beyond loopback.
"""
from __future__ import annotations

import contextlib
import signal
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
REPO_ROOT = Path(__file__).resolve().parent.parent
PEPPER_ENV = {
    "LLM_WIKI_PEPPER": "1:pepper-one",
    "LLM_WIKI_BOOTSTRAP_ADMIN_TOKEN": "bootstrap-secret",
    "PYTHONPATH": str(REPO_ROOT / "skills" / "llm-wiki"),
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


def _plain_get(port: int, path: str, headers: dict[bytes, bytes] | None = None) -> bytes:
    """One plain-HTTP request on loopback, written as raw bytes so a
    test can send a header value no HTTP client library would encode."""
    request = f"GET {path} HTTP/1.1\r\nHost: x\r\n".encode()
    for name, value in (headers or {}).items():
        request += name + b": " + value + b"\r\n"
    request += b"Connection: close\r\n\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
        sock.send(request)
        reply = b""
        while chunk := sock.recv(400):
            reply += chunk
    return reply


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

    def _write(self, port: int, tls_section: str | None = None) -> Path:
        """A deployment file on `port`. `tls_section` replaces the whole
        `[tls]` block, and `None` removes it, so a test can drive a mode
        the spec does not define without the file being malformed."""
        if tls_section is None:
            tls_section = ""
        path = self.root / "deploy.toml"
        path.write_text(
            f"""
[server]
bind = "127.0.0.1:{port}"
state = "{self.state}"

{tls_section}

[access]
read = "open"
write = "token"
admin = "token"

[kbs.demo]
path = "{self.kb}"
"""
        )
        return path

    def _terminate_tls(self, extra: str = "") -> str:
        return (
            f'[tls]\nmode = "terminate"\ncert = "{self.cert}"\n'
            f'key = "{self.key}"\n{extra}'
        )

    def _run(self, deploy: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PYTHON, "-m", "llmwiki_service", str(deploy)],
            cwd=Path(__file__).resolve().parent.parent,
            env=PEPPER_ENV,
            capture_output=True, text=True, timeout=10,
        )

    def _start(self, deploy: Path) -> subprocess.Popen:
        proc = subprocess.Popen(
            [PYTHON, "-m", "llmwiki_service", str(deploy)],
            cwd=Path(__file__).resolve().parent.parent,
            env=PEPPER_ENV,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(self._stop, proc)
        return proc

    @staticmethod
    def _stop(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        if proc.stdout is not None and not proc.stdout.closed:
            proc.stdout.close()

    def test_a_clean_deployment_serves_health_over_tls(self) -> None:
        port = _free_port()
        deploy = self._write(port, self._terminate_tls())
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
        deploy = self._write(port, self._terminate_tls())
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

    def test_no_tls_section_refuses_and_binds_nothing(self) -> None:
        """Row 7 on a running service. Before it existed this file bound
        the port and answered `/health` over plain HTTP, with phase 1's
        bearer tokens crossing the network in the clear."""
        port = _free_port()
        proc = self._run(self._write(port, None))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("[tls] mode", proc.stderr)
        self.assertFalse(_port_open(port))

    def test_every_unrecognised_mode_refuses_and_binds_nothing(self) -> None:
        for mode in ("Terminate", "upstrem", ""):
            with self.subTest(mode=mode):
                port = _free_port()
                deploy = self._write(
                    port,
                    f'[tls]\nmode = "{mode}"\ncert = "{self.cert}"\n'
                    f'key = "{self.key}"\n',
                )
                proc = self._run(deploy)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("[tls] mode", proc.stderr)
                self.assertIn(repr(mode), proc.stderr)
                self.assertFalse(_port_open(port))

    def test_a_boot_fault_prints_one_line_and_binds_nothing(self) -> None:
        """The four faults that used to exit by traceback. Each names its
        setting or its path, on one line, with nothing bound."""
        port = _free_port()
        good = self._write(port, self._terminate_tls()).read_text()

        other_dir = self.root / "other"
        other_dir.mkdir()
        _other_cert, other_key = _make_cert(other_dir)

        cases = {
            "[tls] min_version": self._write(
                port, self._terminate_tls('min_version = "1.1"\n')
            ).read_text(),
            "[server] bind": good.replace(f'bind = "127.0.0.1:{port}"', ""),
            "[tls] cert and key do not load": good.replace(
                f'key = "{self.key}"', f'key = "{other_key}"'
            ),
            "malformed deployment file": "[server\nbind = ",
        }

        for index, (expected, text) in enumerate(cases.items()):
            with self.subTest(fault=expected):
                deploy = self.root / f"fault-{index}.toml"
                deploy.write_text(text)
                proc = self._run(deploy)
                self.assertNotEqual(proc.returncode, 0)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(len(proc.stderr.strip().splitlines()), 1)
                self.assertIn(expected, proc.stderr)
                self.assertFalse(_port_open(port))

    def test_upstream_mode_survives_sighup(self) -> None:
        """SIGHUP was installed only in terminate mode, so it killed an
        upstream-mode service, and the phase teaches SIGHUP as the way to
        force a reload."""
        port = _free_port()
        deploy = self._write(
            port, '[tls]\nmode = "upstream"\ntrusted_proxy = "127.0.0.1"\n'
        )
        proc = self._start(deploy)
        self._wait_for_port(port, proc)
        proc.send_signal(signal.SIGHUP)
        time.sleep(0.5)
        self.assertIsNone(proc.poll(), "SIGHUP killed the service")
        self.assertIn(b"200", _plain_get(port, "/health"))

    def test_the_rightmost_forwarded_element_is_the_client(self) -> None:
        """Through a real uvicorn, not a TestClient: two requests forging
        different leftmost elements from behind the trusted proxy resolve
        to the same client, and a forged rightmost element that is not an
        address falls back to the transport peer."""
        port = _free_port()
        deploy = self._write(
            port, '[tls]\nmode = "upstream"\ntrusted_proxy = "127.0.0.1"\n'
        )
        proc = self._start(deploy)
        self._wait_for_port(port, proc)
        for forged in (b"6.6.6.6, 203.0.113.9", b"9.9.9.9, 203.0.113.9"):
            _plain_get(port, "/health", {b"X-Forwarded-For": forged})
        _plain_get(port, "/health", {b"X-Forwarded-For": b"6.6.6.6, not-an-address"})
        _plain_get(
            port, "/health",
            {b"X-Forwarded-For": b"\xff", b"X-Forwarded-Proto": b"gopher"},
        )
        proc.terminate()
        proc.wait(timeout=5)
        log = proc.stdout.read()
        proc.stdout.close()
        lines = [line for line in log.splitlines() if "client=" in line]
        self.assertEqual(len(lines), 4, lines)
        self.assertEqual(lines[0].count("203.0.113.9"), 1)
        self.assertEqual(lines[1].count("203.0.113.9"), 1)
        for line in lines:
            self.assertNotIn("6.6.6.6", line)
            self.assertNotIn("9.9.9.9", line)
        self.assertIn("127.0.0.1", lines[2])
        self.assertIn("127.0.0.1", lines[3])
        self.assertIn("scheme=http ", lines[3])

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
