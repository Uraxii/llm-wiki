"""The deployment file parser and the 14-row startup refusal table."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class TokenDbPathTest(unittest.TestCase):
    def test_no_server_table_gives_no_path(self) -> None:
        self.assertIsNone(deployment._token_db_path({}))

    def test_state_path_gets_the_token_db_name_appended(self) -> None:
        path = deployment._token_db_path({"server": {"state": "/var/lib/x"}})
        self.assertEqual(path, Path("/var/lib/x") / deployment.TOKEN_DB_NAME)


class BindIsPrivateTest(unittest.TestCase):
    def test_an_unparseable_host_is_not_private(self) -> None:
        self.assertFalse(deployment._bind_is_private("not-an-ip:8443"))

    def test_ipv6_loopback_in_brackets_is_private(self) -> None:
        self.assertTrue(deployment._bind_is_private("[::1]:8443"))

    def test_only_brackets_are_stripped_not_arbitrary_characters(self) -> None:
        """The docstring names brackets specifically, for IPv6 hosts.
        Stripping a wider character set would let an address like this
        pass as private by accident."""
        self.assertFalse(deployment._bind_is_private("X10.0.0.5:8443"))


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


class AdminRowExistsTest(DeploymentTestBase):
    """`_admin_row_exists` backs row 5's fallback: an existing admin row
    lets a deployment start without the bootstrap token."""

    def test_no_state_configured_reads_as_no_admin_row(self) -> None:
        self.assertFalse(deployment._admin_row_exists({}))

    def test_missing_database_reads_as_no_admin_row(self) -> None:
        self.assertFalse(
            deployment._admin_row_exists(self.terminate_deployment())
        )

    def test_an_existing_admin_row_is_found(self) -> None:
        store = tokens.TokenStore(
            self.state / deployment.TOKEN_DB_NAME, tokens.Peppers({1: b"pepper-one"})
        )
        store.mint("first-admin", "admin")
        self.assertTrue(
            deployment._admin_row_exists(self.terminate_deployment())
        )

    def test_a_corrupt_token_database_reads_as_no_admin_row(self) -> None:
        """A row 5 check that mistakes a corrupt database for a live
        admin row would let an unadministrable service start."""
        depl = self.terminate_deployment()
        (self.state / deployment.TOKEN_DB_NAME).write_bytes(b"not a database")
        self.assertFalse(deployment._admin_row_exists(depl))


class AdminBootstrapCheckTest(DeploymentTestBase):
    def test_no_token_and_no_admin_row_refused(self) -> None:
        environ = base_environ()
        del environ[ADMIN_ENV]
        message = deployment._check_admin_bootstrap(
            self.terminate_deployment(), environ
        )
        self.assertEqual(
            message,
            f"no admin access: set {ADMIN_ENV}, or this deployment has no "
            "existing admin token to mint one with",
        )

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
        self.assertEqual(
            message,
            '[access] write = "open" is refused; write is always "token"',
        )

    def test_write_token_is_not_refused(self) -> None:
        self.assertIsNone(
            deployment._check_write_open(self.terminate_deployment(), {})
        )

    def test_no_access_table_is_not_refused(self) -> None:
        self.assertIsNone(deployment._check_write_open({}, {}))

    def test_a_typo_is_not_treated_as_open(self) -> None:
        """Acceptance: only the exact word "open" trips this refusal.
        A typo must not slip past it by accident, but it must also not
        be silently treated as if it meant "open" here."""
        depl = self.terminate_deployment()
        for typo in ("Open", "OPEN", "opn", " open"):
            with self.subTest(typo=typo):
                depl["access"]["write"] = typo
                self.assertIsNone(deployment._check_write_open(depl, {}))


class AdminOpenCheckTest(DeploymentTestBase):
    """Row 14. The gate ignores `[access] admin`, so a file that sets it
    to "open" describes a service that does not exist."""

    def test_admin_open_refused(self) -> None:
        depl = self.terminate_deployment()
        depl["access"]["admin"] = "open"
        message = deployment._check_admin_open(depl, {})
        self.assertEqual(
            message,
            '[access] admin = "open" is refused; the gate opens only read, '
            "so this describes a service that does not exist",
        )

    def test_admin_token_is_not_refused(self) -> None:
        self.assertIsNone(
            deployment._check_admin_open(self.terminate_deployment(), {})
        )

    def test_no_access_table_is_not_refused(self) -> None:
        self.assertIsNone(deployment._check_admin_open({}, {}))

    def test_a_typo_is_not_treated_as_open(self) -> None:
        depl = self.terminate_deployment()
        for typo in ("Open", "OPEN", "opn", " open"):
            with self.subTest(typo=typo):
                depl["access"]["admin"] = typo
                self.assertIsNone(deployment._check_admin_open(depl, {}))

    def test_read_open_does_not_trip_the_admin_row(self) -> None:
        depl = self.terminate_deployment()
        depl["access"]["read"] = "open"
        self.assertIsNone(deployment._check_admin_open(depl, {}))

    def test_the_whole_deployment_is_refused_by_setting_name(self) -> None:
        depl = self.terminate_deployment()
        depl["server"]["bind"] = "127.0.0.1:8443"
        depl["access"]["admin"] = "open"
        refusals = deployment.check_deployment(depl, base_environ())
        self.assertEqual(len(refusals), 1)
        self.assertIn('[access] admin = "open"', refusals[0])


class BootstrapAdminTokenTest(unittest.TestCase):
    """The one reader of the bootstrap variable, shared by row 5 and
    `auth.authorize_admin`."""

    def test_a_set_value_is_returned(self) -> None:
        self.assertEqual(
            deployment.bootstrap_admin_token({ADMIN_ENV: "secret"}), "secret"
        )

    def test_unset_is_none(self) -> None:
        self.assertIsNone(deployment.bootstrap_admin_token({}))

    def test_empty_is_none_not_an_empty_credential(self) -> None:
        """An empty string reaching the gate would be a credential an
        empty header matches."""
        self.assertIsNone(deployment.bootstrap_admin_token({ADMIN_ENV: ""}))

    def test_row_five_reads_it_through_this_function(self) -> None:
        self.assertIsNotNone(
            deployment._check_admin_bootstrap({}, {ADMIN_ENV: ""})
        )
        self.assertIsNone(
            deployment._check_admin_bootstrap({}, {ADMIN_ENV: "secret"})
        )


class TlsModeCheckTest(DeploymentTestBase):
    """Row 7. Before this row existed, every mode-conditioned check
    short-circuited on a mode it did not recognise and `_serve` fell
    through to a plaintext listener, so a typo or a missing `[tls]`
    section served bearer tokens in the clear on whatever `bind` named.
    """

    def test_the_two_defined_modes_are_not_refused(self) -> None:
        self.assertIsNone(deployment.tls_mode_refusal(self.terminate_deployment()))
        self.assertIsNone(deployment.tls_mode_refusal(self.upstream_deployment()))

    def test_no_tls_section_at_all_is_refused(self) -> None:
        message = deployment.tls_mode_refusal({"server": {"bind": "0.0.0.0:8443"}})
        self.assertIn("[tls] mode", message)

    def test_every_unrecognised_mode_is_refused_by_name(self) -> None:
        depl = self.terminate_deployment()
        for mode in ("Terminate", "upstrem", "", "TERMINATE", " terminate", "none"):
            with self.subTest(mode=mode):
                depl["tls"]["mode"] = mode
                message = deployment.tls_mode_refusal(depl)
                self.assertIsNotNone(message)
                self.assertIn("[tls] mode", message)
                self.assertIn(repr(mode), message)

    def test_tls_mode_reads_only_the_two_defined_values(self) -> None:
        self.assertEqual(
            deployment.tls_mode(self.terminate_deployment()), deployment.TERMINATE
        )
        self.assertEqual(
            deployment.tls_mode(self.upstream_deployment()), deployment.UPSTREAM
        )
        self.assertIsNone(deployment.tls_mode({"tls": {"mode": "Terminate"}}))
        self.assertIsNone(deployment.tls_mode({}))

    def test_an_unrecognised_mode_does_not_switch_the_other_rows_off(self) -> None:
        """The bypass itself: with `mode` unrecognised, the four
        mode-conditioned rows stay silent, so row 7 has to be the one
        that speaks or the deployment reads as clean."""
        depl = self.terminate_deployment()
        depl["tls"]["mode"] = "Terminate"
        depl["tls"]["cert"] = str(self.root / "nope.pem")
        refusals = deployment.check_deployment(depl, base_environ())
        self.assertTrue(any("[tls] mode" in r for r in refusals), refusals)


class TrustedProxyAddressTest(DeploymentTestBase):
    """The one predicate row 11 and `ForwardedHeaderMiddleware` share."""

    def test_an_address_parses(self) -> None:
        depl = self.upstream_deployment(trusted_proxy="10.0.0.1")
        self.assertEqual(
            str(deployment.trusted_proxy_address(depl)), "10.0.0.1"
        )

    def test_a_hostname_is_not_an_address(self) -> None:
        depl = self.upstream_deployment(trusted_proxy="ingress.internal")
        self.assertIsNone(deployment.trusted_proxy_address(depl))

    def test_unset_is_not_an_address(self) -> None:
        self.assertIsNone(deployment.trusted_proxy_address({}))
        self.assertIsNone(deployment.trusted_proxy_address(self.upstream_deployment()))


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

    def test_no_tls_table_is_not_refused(self) -> None:
        self.assertIsNone(deployment._check_cert_files({}, {}))


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

    def test_no_tls_table_is_not_refused(self) -> None:
        self.assertIsNone(deployment._check_cert_expiry({}, {}))

    def test_a_cert_path_that_check_cert_files_already_caught_is_skipped(self) -> None:
        """`_check_cert_files` already refuses a missing cert; this
        function must not also try to decode a file that is not there."""
        depl = self.terminate_deployment()
        depl["tls"]["cert"] = str(self.root / "nope.pem")
        self.assertIsNone(deployment._check_cert_expiry(depl, {}))

    def test_a_cert_expiring_at_exactly_this_instant_is_refused(self) -> None:
        with (
            mock.patch.object(deployment.time, "time", return_value=1000.0),
            mock.patch.object(
                deployment.ssl, "cert_time_to_seconds", return_value=1000.0
            ),
        ):
            message = deployment._check_cert_expiry(self.terminate_deployment(), {})
        self.assertIsNotNone(message)


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

    def test_no_tls_table_is_not_refused(self) -> None:
        self.assertIsNone(deployment._check_upstream_bind({}, {}))

    def test_upstream_mode_with_no_server_table(self) -> None:
        depl = {"tls": {"mode": "upstream"}}
        self.assertEqual(
            deployment._check_upstream_bind(depl, {}),
            "[server] bind is not set",
        )


class OpenReadNeedsProxyCheckTest(DeploymentTestBase):
    def test_open_read_upstream_no_proxy_refused(self) -> None:
        depl = self.upstream_deployment()
        message = deployment._check_open_read_needs_proxy(depl, {})
        self.assertEqual(
            message,
            '[access] read = "open" needs [tls] trusted_proxy set under '
            'mode = "upstream", or every caller shares one rate limit',
        )

    def test_no_tls_or_access_table_is_not_refused(self) -> None:
        self.assertIsNone(deployment._check_open_read_needs_proxy({}, {}))

    def test_open_read_with_no_tls_table_is_not_refused(self) -> None:
        """`read = "open"` alone must not crash when `[tls]` is absent:
        the mode check still has to run before this refusal applies."""
        depl = {"access": {"read": "open"}}
        self.assertIsNone(deployment._check_open_read_needs_proxy(depl, {}))

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

    def test_a_hostname_trusted_proxy_is_refused_by_name(self) -> None:
        """A hostname passed this row on truthiness while the middleware
        ignored it forever, so the deployment read as clean in exactly
        the state this row exists to prevent."""
        depl = self.upstream_deployment(trusted_proxy="ingress.internal")
        self.assertEqual(
            deployment._check_open_read_needs_proxy(depl, {}),
            "[tls] trusted_proxy is not an IP address: 'ingress.internal'; it "
            "is compared against the transport peer, which is always a literal",
        )

    def test_an_ipv6_trusted_proxy_satisfies_it(self) -> None:
        depl = self.upstream_deployment(trusted_proxy="fd00::1")
        self.assertIsNone(deployment._check_open_read_needs_proxy(depl, {}))

    def test_a_typo_on_read_does_not_trip_it(self) -> None:
        depl = self.upstream_deployment()
        depl["access"]["read"] = "opn"
        self.assertIsNone(deployment._check_open_read_needs_proxy(depl, {}))


class StateWritableCheckTest(DeploymentTestBase):
    def test_missing_state_setting_refused(self) -> None:
        depl = self.terminate_deployment()
        del depl["server"]["state"]
        self.assertEqual(
            deployment._check_state_writable(depl, {}),
            "[server] state is not set",
        )

    def test_no_server_table_is_refused_as_state_not_set(self) -> None:
        self.assertEqual(
            deployment._check_state_writable({}, {}),
            "[server] state is not set",
        )

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

    def test_no_kbs_table_is_not_refused(self) -> None:
        self.assertIsNone(deployment._check_kbs_writable({}, {}))


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

    def test_registry_covers_exactly_the_eleven_parsed_dict_rows(self) -> None:
        """Rows 4 to 14, in the table's own order. Row 14 is appended
        rather than placed beside row 6, its natural neighbour, because
        every `Check` carries the row number the phase 2 table gives it
        and inserting in the middle would renumber seven of them."""
        self.assertEqual([check.row for check in deployment.REGISTRY], list(range(4, 15)))


class StartupRefusalsTest(DeploymentTestBase):
    def test_no_argument_short_circuits_the_registry(self) -> None:
        self.assertEqual(
            deployment.startup_refusals(None, base_environ()),
            ([deployment.pre_parse_refusal(None)], {}),
        )

    def test_a_clean_deployment_file_passes(self) -> None:
        depl = self.terminate_deployment()
        depl["server"]["bind"] = "127.0.0.1:8443"
        path = self.root / "deploy.toml"
        path.write_text(_toml(depl))
        refusals, parsed = deployment.startup_refusals(str(path), base_environ())
        self.assertEqual(refusals, [])
        # The caller serves this dict rather than reopening the file, so
        # the bytes that get served are the bytes that were checked.
        self.assertEqual(parsed, deployment.load_deployment(str(path)))

    def test_a_faulty_deployment_file_is_refused_by_setting_name(self) -> None:
        depl = self.terminate_deployment()
        depl["access"]["write"] = "open"
        path = self.root / "deploy.toml"
        path.write_text(_toml(depl))
        refusals, _parsed = deployment.startup_refusals(str(path), base_environ())
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
