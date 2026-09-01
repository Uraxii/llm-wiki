"""The deployment file parser and the 13-row startup refusal table."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from llmwiki_service import deployment, tokens

PEPPER = "1:pepper-one"
ADMIN_ENV = deployment.BOOTSTRAP_ADMIN_TOKEN_ENV_VAR


def base_environ(**overrides: str) -> dict[str, str]:
    environ = {tokens.PEPPER_ENV_VAR: PEPPER, ADMIN_ENV: "bootstrap-secret"}
    environ.update(overrides)
    return environ


def make_cert(dest: Path, name: str, *, not_before: str, not_after: str) -> Path:
    """A minimal self-signed EC cert and key, generated locally with the
    system `openssl` binary: no network, no checked-in fixture."""
    cert = dest / f"{name}-cert.pem"
    key = dest / f"{name}-key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:P-256",
            "-keyout", str(key), "-out", str(cert),
            "-nodes", "-subj", "/CN=test",
            "-not_before", not_before, "-not_after", not_after,
        ],
        check=True, capture_output=True,
    )
    return cert, key


class DeploymentTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)

        self.state = self.root / "state"
        self.state.mkdir()
        self.kb = self.root / "kb"
        self.kb.mkdir()

        self.valid_cert, self.valid_key = make_cert(
            self.root, "valid",
            not_before="20260101000000Z", not_after="20991231000000Z",
        )
        self.expired_cert, self.expired_key = make_cert(
            self.root, "expired",
            not_before="20200101000000Z", not_after="20200102000000Z",
        )
        self.garbage = self.root / "garbage.pem"
        self.garbage.write_text("not a certificate\n")

    def terminate_deployment(self) -> dict:
        return {
            "server": {"bind": "0.0.0.0:8443", "state": str(self.state)},
            "tls": {
                "mode": "terminate",
                "cert": str(self.valid_cert),
                "key": str(self.valid_key),
            },
            "access": {"read": "open", "write": "token", "admin": "token"},
            "kbs": {"demo": {"path": str(self.kb)}},
        }

    def upstream_deployment(self, **tls_overrides: object) -> dict:
        depl = self.terminate_deployment()
        depl["tls"] = {"mode": "upstream", **tls_overrides}
        return depl


class PreParseRefusalTest(unittest.TestCase):
    """The three refusals that fire before the file is parsed."""

    def test_no_argument_refused(self) -> None:
        message = deployment.pre_parse_refusal(None)
        self.assertIn("no deployment file", message)

    def test_missing_path_refused(self) -> None:
        message = deployment.pre_parse_refusal("/does/not/exist.toml")
        self.assertIn("/does/not/exist.toml", message)

    def test_a_directory_cannot_be_read_as_a_deployment_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            message = deployment.pre_parse_refusal(tmp)
        self.assertIn(tmp, message)
        self.assertIn("cannot be read", message)

    def test_mode_000_file_cannot_be_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deploy.toml"
            path.write_text("[server]\n")
            path.chmod(0o000)
            try:
                message = deployment.pre_parse_refusal(str(path))
            finally:
                path.chmod(0o644)
        self.assertIn(str(path), message)
        self.assertIn("cannot be read", message)

    def test_a_readable_file_is_not_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deploy.toml"
            path.write_text("[server]\n")
            self.assertIsNone(deployment.pre_parse_refusal(str(path)))


class LoadDeploymentTest(unittest.TestCase):
    def test_parses_to_a_plain_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deploy.toml"
            path.write_text('[server]\nbind = "0.0.0.0:8443"\n')
            parsed = deployment.load_deployment(str(path))
        self.assertEqual(parsed, {"server": {"bind": "0.0.0.0:8443"}})
        self.assertIs(type(parsed), dict)

    def test_malformed_toml_raises_naming_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deploy.toml"
            path.write_text("this is not [ valid toml")
            with self.assertRaises(ValueError) as caught:
                deployment.load_deployment(str(path))
        self.assertIn(str(path), str(caught.exception))

    def test_empty_access_table_parses_and_is_all_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deploy.toml"
            path.write_text("[access]\n")
            parsed = deployment.load_deployment(str(path))
        self.assertEqual(parsed, {"access": {}})


class PepperCheckTest(DeploymentTestBase):
    def test_no_pepper_refused(self) -> None:
        environ = base_environ()
        del environ[tokens.PEPPER_ENV_VAR]
        message = deployment._check_pepper(self.terminate_deployment(), environ)
        self.assertIsNotNone(message)
        self.assertIn(tokens.PEPPER_ENV_VAR, message)

    def test_a_pepper_is_not_refused(self) -> None:
        self.assertIsNone(
            deployment._check_pepper(self.terminate_deployment(), base_environ())
        )


class AdminBootstrapCheckTest(DeploymentTestBase):
    def test_no_token_and_no_admin_row_refused(self) -> None:
        environ = base_environ()
        del environ[ADMIN_ENV]
        message = deployment._check_admin_bootstrap(
            self.terminate_deployment(), environ
        )
        self.assertIsNotNone(message)
        self.assertIn(ADMIN_ENV, message)

    def test_bootstrap_token_alone_satisfies_it(self) -> None:
        self.assertIsNone(
            deployment._check_admin_bootstrap(
                self.terminate_deployment(), base_environ()
            )
        )

    def test_an_existing_admin_row_satisfies_it_without_the_token(self) -> None:
        store = tokens.TokenStore(
            self.state / deployment.TOKEN_DB_NAME, tokens.Peppers({1: b"pepper-one"})
        )
        store.mint("first-admin", "admin")
        environ = base_environ()
        del environ[ADMIN_ENV]
        self.assertIsNone(
            deployment._check_admin_bootstrap(self.terminate_deployment(), environ)
        )

    def test_a_revoked_admin_row_does_not_satisfy_it(self) -> None:
        store = tokens.TokenStore(
            self.state / deployment.TOKEN_DB_NAME, tokens.Peppers({1: b"pepper-one"})
        )
        store.mint("first-admin", "admin")
        store.revoke("first-admin")
        environ = base_environ()
        del environ[ADMIN_ENV]
        self.assertIsNotNone(
            deployment._check_admin_bootstrap(self.terminate_deployment(), environ)
        )


class WriteOpenCheckTest(DeploymentTestBase):
    def test_write_open_refused(self) -> None:
        depl = self.terminate_deployment()
        depl["access"]["write"] = "open"
        message = deployment._check_write_open(depl, {})
        self.assertIsNotNone(message)
        self.assertIn("write", message)

    def test_write_token_is_not_refused(self) -> None:
        self.assertIsNone(
            deployment._check_write_open(self.terminate_deployment(), {})
        )

    def test_a_typo_is_not_treated_as_open(self) -> None:
        """Acceptance: only the exact word "open" trips this refusal.
        A typo must not slip past it by accident, but it must also not
        be silently treated as if it meant "open" here."""
        depl = self.terminate_deployment()
        for typo in ("Open", "OPEN", "opn", " open"):
            with self.subTest(typo=typo):
                depl["access"]["write"] = typo
                self.assertIsNone(deployment._check_write_open(depl, {}))


class CertFilesCheckTest(DeploymentTestBase):
    def test_missing_cert_refused(self) -> None:
        depl = self.terminate_deployment()
        depl["tls"]["cert"] = str(self.root / "nope.pem")
        message = deployment._check_cert_files(depl, {})
        self.assertIsNotNone(message)
        self.assertIn("cert", message)

    def test_missing_key_refused(self) -> None:
        depl = self.terminate_deployment()
        depl["tls"]["key"] = str(self.root / "nope-key.pem")
        message = deployment._check_cert_files(depl, {})
        self.assertIsNotNone(message)
        self.assertIn("key", message)

    def test_unreadable_cert_refused(self) -> None:
        depl = self.terminate_deployment()
        os.chmod(self.valid_cert, 0o000)
        try:
            message = deployment._check_cert_files(depl, {})
        finally:
            os.chmod(self.valid_cert, 0o644)
        self.assertIsNotNone(message)

    def test_valid_cert_and_key_not_refused(self) -> None:
        self.assertIsNone(
            deployment._check_cert_files(self.terminate_deployment(), {})
        )

    def test_upstream_mode_skips_this_check(self) -> None:
        depl = self.upstream_deployment()
        self.assertIsNone(deployment._check_cert_files(depl, {}))


class CertExpiryCheckTest(DeploymentTestBase):
    def test_expired_cert_refused(self) -> None:
        depl = self.terminate_deployment()
        depl["tls"]["cert"] = str(self.expired_cert)
        message = deployment._check_cert_expiry(depl, {})
        self.assertIsNotNone(message)
        self.assertIn("expired", message)

    def test_unexpired_cert_not_refused(self) -> None:
        self.assertIsNone(
            deployment._check_cert_expiry(self.terminate_deployment(), {})
        )

    def test_a_file_that_is_not_a_certificate_is_refused(self) -> None:
        depl = self.terminate_deployment()
        depl["tls"]["cert"] = str(self.garbage)
        message = deployment._check_cert_expiry(depl, {})
        self.assertIsNotNone(message)
        self.assertIn(str(self.garbage), message)

    def test_upstream_mode_skips_this_check(self) -> None:
        self.assertIsNone(deployment._check_cert_expiry(self.upstream_deployment(), {}))


class UpstreamBindCheckTest(DeploymentTestBase):
    def test_any_interface_bind_refused(self) -> None:
        depl = self.upstream_deployment()
        depl["server"]["bind"] = "0.0.0.0:8443"
        message = deployment._check_upstream_bind(depl, {})
        self.assertIsNotNone(message)
        self.assertIn("0.0.0.0:8443", message)

    def test_ipv6_any_interface_bind_refused(self) -> None:
        depl = self.upstream_deployment()
        depl["server"]["bind"] = "[::]:8443"
        self.assertIsNotNone(deployment._check_upstream_bind(depl, {}))

    def test_public_address_bind_refused(self) -> None:
        depl = self.upstream_deployment()
        depl["server"]["bind"] = "1.2.3.4:8443"
        self.assertIsNotNone(deployment._check_upstream_bind(depl, {}))

    def test_loopback_bind_not_refused(self) -> None:
        depl = self.upstream_deployment()
        depl["server"]["bind"] = "127.0.0.1:8443"
        self.assertIsNone(deployment._check_upstream_bind(depl, {}))

    def test_private_address_bind_not_refused(self) -> None:
        depl = self.upstream_deployment()
        depl["server"]["bind"] = "10.0.0.5:8443"
        self.assertIsNone(deployment._check_upstream_bind(depl, {}))

    def test_missing_bind_refused(self) -> None:
        depl = self.upstream_deployment()
        del depl["server"]["bind"]
        self.assertIsNotNone(deployment._check_upstream_bind(depl, {}))

    def test_terminate_mode_skips_this_check(self) -> None:
        depl = self.terminate_deployment()
        depl["server"]["bind"] = "0.0.0.0:8443"
        self.assertIsNone(deployment._check_upstream_bind(depl, {}))


class OpenReadNeedsProxyCheckTest(DeploymentTestBase):
    def test_open_read_upstream_no_proxy_refused(self) -> None:
        depl = self.upstream_deployment()
        message = deployment._check_open_read_needs_proxy(depl, {})
        self.assertIsNotNone(message)
        self.assertIn("trusted_proxy", message)

    def test_trusted_proxy_satisfies_it(self) -> None:
        depl = self.upstream_deployment(trusted_proxy="10.0.0.1")
        self.assertIsNone(deployment._check_open_read_needs_proxy(depl, {}))

    def test_closed_read_does_not_need_a_proxy(self) -> None:
        depl = self.upstream_deployment()
        depl["access"]["read"] = "token"
        self.assertIsNone(deployment._check_open_read_needs_proxy(depl, {}))

    def test_terminate_mode_does_not_need_a_proxy(self) -> None:
        self.assertIsNone(
            deployment._check_open_read_needs_proxy(self.terminate_deployment(), {})
        )

    def test_a_typo_on_read_does_not_trip_it(self) -> None:
        depl = self.upstream_deployment()
        depl["access"]["read"] = "opn"
        self.assertIsNone(deployment._check_open_read_needs_proxy(depl, {}))


class StateWritableCheckTest(DeploymentTestBase):
    def test_missing_state_setting_refused(self) -> None:
        depl = self.terminate_deployment()
        del depl["server"]["state"]
        self.assertIsNotNone(deployment._check_state_writable(depl, {}))

    def test_nonexistent_state_dir_refused(self) -> None:
        depl = self.terminate_deployment()
        depl["server"]["state"] = str(self.root / "no-such-dir")
        self.assertIsNotNone(deployment._check_state_writable(depl, {}))

    def test_read_only_state_dir_refused(self) -> None:
        depl = self.terminate_deployment()
        os.chmod(self.state, 0o500)
        try:
            message = deployment._check_state_writable(depl, {})
        finally:
            os.chmod(self.state, 0o700)
        self.assertIsNotNone(message)

    def test_writable_state_dir_not_refused(self) -> None:
        self.assertIsNone(
            deployment._check_state_writable(self.terminate_deployment(), {})
        )


class KbsWritableCheckTest(DeploymentTestBase):
    def test_nonexistent_kb_path_refused(self) -> None:
        depl = self.terminate_deployment()
        depl["kbs"]["demo"]["path"] = str(self.root / "no-such-kb")
        message = deployment._check_kbs_writable(depl, {})
        self.assertIsNotNone(message)
        self.assertIn("demo", message)

    def test_read_only_kb_path_refused(self) -> None:
        depl = self.terminate_deployment()
        os.chmod(self.kb, 0o500)
        try:
            message = deployment._check_kbs_writable(depl, {})
        finally:
            os.chmod(self.kb, 0o700)
        self.assertIsNotNone(message)

    def test_writable_kb_path_not_refused(self) -> None:
        self.assertIsNone(
            deployment._check_kbs_writable(self.terminate_deployment(), {})
        )


class KbsLegacyEndpointCheckTest(DeploymentTestBase):
    def test_legacy_endpoint_section_refused(self) -> None:
        (self.kb / "config.toml").write_text('[endpoint]\nurl = "http://x"\n')
        depl = self.terminate_deployment()
        message = deployment._check_kbs_legacy_endpoint(depl, {})
        self.assertIsNotNone(message)
        self.assertIn("demo", message)
        self.assertIn(str(self.kb), message)

    def test_no_config_toml_not_refused(self) -> None:
        self.assertIsNone(
            deployment._check_kbs_legacy_endpoint(self.terminate_deployment(), {})
        )

    def test_config_toml_without_endpoint_not_refused(self) -> None:
        (self.kb / "config.toml").write_text('[models]\nsummarize = "x"\n')
        self.assertIsNone(
            deployment._check_kbs_legacy_endpoint(self.terminate_deployment(), {})
        )


class CheckDeploymentTest(DeploymentTestBase):
    def test_a_clean_deployment_has_no_refusals(self) -> None:
        depl = self.terminate_deployment()
        depl["server"]["bind"] = "127.0.0.1:8443"
        self.assertEqual(deployment.check_deployment(depl, base_environ()), [])

    def test_every_row_is_checked_not_just_the_first(self) -> None:
        """Five faults at once: every one must be named, not just the
        first hit."""
        depl = self.terminate_deployment()
        depl["access"]["write"] = "open"
        depl["tls"]["cert"] = str(self.root / "missing.pem")
        os.chmod(self.state, 0o500)
        os.chmod(self.kb, 0o500)
        self.addCleanup(os.chmod, self.state, 0o700)
        self.addCleanup(os.chmod, self.kb, 0o700)
        environ = base_environ()
        del environ[tokens.PEPPER_ENV_VAR]

        refusals = deployment.check_deployment(depl, environ)

        self.assertGreaterEqual(len(refusals), 5)
        joined = "\n".join(refusals)
        self.assertIn(tokens.PEPPER_ENV_VAR, joined)
        self.assertIn("write", joined)
        self.assertIn("missing.pem", joined)
        self.assertIn(str(self.state), joined)
        self.assertIn(str(self.kb), joined)

    def test_registry_covers_exactly_the_ten_parsed_dict_rows(self) -> None:
        self.assertEqual([check.row for check in deployment.REGISTRY], list(range(4, 14)))


class StartupRefusalsTest(DeploymentTestBase):
    def test_no_argument_short_circuits_the_registry(self) -> None:
        self.assertEqual(
            deployment.startup_refusals(None, base_environ()),
            [deployment.pre_parse_refusal(None)],
        )

    def test_a_clean_deployment_file_passes(self) -> None:
        depl = self.terminate_deployment()
        depl["server"]["bind"] = "127.0.0.1:8443"
        path = self.root / "deploy.toml"
        path.write_text(_toml(depl))
        self.assertEqual(deployment.startup_refusals(str(path), base_environ()), [])

    def test_a_faulty_deployment_file_is_refused_by_setting_name(self) -> None:
        depl = self.terminate_deployment()
        depl["access"]["write"] = "open"
        path = self.root / "deploy.toml"
        path.write_text(_toml(depl))
        refusals = deployment.startup_refusals(str(path), base_environ())
        self.assertTrue(any("write" in r for r in refusals))


def _toml(value: object, indent: str = "") -> str:
    """Render a nested dict of strings back to TOML for a round-trip
    test. Only handles the shapes this test file builds: strings inside
    one or two levels of table nesting, which is all a deployment file
    needs."""
    lines = []
    tables = {k: v for k, v in value.items() if isinstance(v, dict)}
    scalars = {k: v for k, v in value.items() if not isinstance(v, dict)}
    for key, scalar in scalars.items():
        lines.append(f'{key} = "{scalar}"')
    for key, table in tables.items():
        nested = {k: v for k, v in table.items() if isinstance(v, dict)}
        flat = {k: v for k, v in table.items() if not isinstance(v, dict)}
        lines.append(f"\n[{key}]")
        for k, v in flat.items():
            lines.append(f'{k} = "{v}"')
        for k, v in nested.items():
            lines.append(f"\n[{key}.{k}]")
            for kk, vv in v.items():
                lines.append(f'{kk} = "{vv}"')
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    unittest.main()
