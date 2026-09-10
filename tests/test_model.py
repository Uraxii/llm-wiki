"""Chat and embed over a configured provider, against the fake. Also
`resolve_target` and `step_is_configured`, the parse from `[models]`
and `[providers]` down to one `ModelTarget`, tested directly over plain
dicts with no HTTP in the way."""

import io
import logging
import os
import shutil
import sys
import tempfile
import traceback
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "llm-wiki"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from llmwiki.model import (  # noqa: E402
    ModelError,
    ModelTarget,
    chat,
    embed,
    resolve_target,
    step_is_configured,
)
from fake_endpoint import FakeEndpoint  # noqa: E402


@contextmanager
def _env(values: dict):
    """Set (or, for a `None` value, delete) each env var for the
    duration, restoring exactly what was there before. The model client
    reads the real environment, so tests must control it precisely
    rather than assume a clean shell."""
    sentinel = object()
    previous = {key: os.environ.get(key, sentinel) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is sentinel:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _target(
    url: str,
    model: str = "chat-model",
    *,
    provider: str = "test",
    key_env: str | None = None,
    key_file_env: str | None = None,
    pdf_part: str = "file",
) -> ModelTarget:
    return ModelTarget(provider, url, model, key_env, key_file_env, pdf_part)


def _ok_chat(_path: str, _body: dict) -> dict:
    return {"choices": [{"message": {"content": "answer text"}}]}


class ChatTest(unittest.TestCase):
    def test_request_shape_and_response(self) -> None:
        with FakeEndpoint(_ok_chat) as fake, _env({"K": "secret-key"}):
            target = _target(fake.url, "chat-model", key_env="K")
            result = chat(target, "hello there")

        self.assertEqual(result, "answer text")
        self.assertEqual(len(fake.requests), 1)
        request = fake.requests[0]
        self.assertEqual(request.path, "/chat/completions")
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            request.body,
            {
                "model": "chat-model",
                "messages": [{"role": "user", "content": "hello there"}],
            },
        )
        self.assertEqual(request.headers.get("Authorization"), "Bearer secret-key")

    def test_temperature_sent_only_when_given(self) -> None:
        """`test_request_shape_and_response` pins the whole body of a
        call that leaves `temperature` unset, so omission is already
        proved there. This pins the other half: passing it adds one
        key and moves nothing else."""
        with FakeEndpoint(_ok_chat) as fake:
            target = _target(fake.url, "judge-model")
            chat(target, "pick one", temperature=0.0)

        self.assertEqual(
            fake.requests[0].body,
            {
                "model": "judge-model",
                "messages": [{"role": "user", "content": "pick one"}],
                "temperature": 0,
            },
        )

    def test_model_is_read_from_the_target(self) -> None:
        captured = {}

        def respond(_path: str, body: dict) -> dict:
            captured["model"] = body["model"]
            return {"choices": [{"message": {"content": "x"}}]}

        with FakeEndpoint(respond) as fake:
            chat(_target(fake.url, "target-model"), "hi")

        self.assertEqual(captured["model"], "target-model")


class EmbedTest(unittest.TestCase):
    def test_orders_by_index_despite_shuffled_response(self) -> None:
        def respond(_path: str, _body: dict) -> dict:
            return {
                "data": [
                    {"index": 2, "embedding": [2.0]},
                    {"index": 0, "embedding": [0.0]},
                    {"index": 1, "embedding": [1.0]},
                ]
            }

        with FakeEndpoint(respond) as fake:
            vectors = embed(_target(fake.url, "embed-model"), ["a", "b", "c"])

        self.assertEqual(vectors, [[0.0], [1.0], [2.0]])

    def test_uses_the_targets_model_and_the_embeddings_path(self) -> None:
        captured = {}

        def respond(path: str, body: dict) -> dict:
            captured["model"] = body["model"]
            captured["path"] = path
            return {"data": [{"index": 0, "embedding": [1.0]}]}

        with FakeEndpoint(respond) as fake:
            embed(_target(fake.url, "embed-model"), ["only"])

        self.assertEqual(captured["model"], "embed-model")
        self.assertEqual(captured["path"], "/embeddings")


class CredentialTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp_dir)

    def _write_key_file(self, content: str) -> Path:
        path = Path(self._tmp_dir) / "key.txt"
        path.write_text(content, encoding="utf-8")
        return path

    def test_key_env_wins_over_key_file_env(self) -> None:
        key_path = self._write_key_file("file-key\n")

        with FakeEndpoint(_ok_chat) as fake, _env(
            {"K": "value-key", "KF": str(key_path)}
        ):
            target = _target(fake.url, key_env="K", key_file_env="KF")
            chat(target, "hi")

        self.assertEqual(
            fake.requests[0].headers.get("Authorization"), "Bearer value-key"
        )

    def test_key_file_env_used_when_key_env_unset_strips_one_newline(self) -> None:
        key_path = self._write_key_file("file-key\n")

        with FakeEndpoint(_ok_chat) as fake, _env({"KF": str(key_path)}):
            target = _target(fake.url, key_file_env="KF")
            chat(target, "hi")

        self.assertEqual(
            fake.requests[0].headers.get("Authorization"), "Bearer file-key"
        )

    def test_neither_field_set_sends_no_authorization_header(self) -> None:
        """A provider that declares neither key_env nor key_file_env is
        what a server on the operator's own machine usually wants: no
        Authorization header, and no error."""
        with FakeEndpoint(_ok_chat) as fake:
            chat(_target(fake.url), "hi")

        self.assertNotIn("Authorization", fake.requests[0].headers)

    def test_key_env_named_but_unset_raises_naming_the_variable(self) -> None:
        with FakeEndpoint(_ok_chat) as fake, _env({"K": None}):
            target = _target(fake.url, key_env="K")
            with self.assertRaises(ModelError) as ctx:
                chat(target, "hi")
        self.assertIn("K", str(ctx.exception))

    def test_key_file_env_named_but_unset_raises_naming_the_variable(self) -> None:
        with FakeEndpoint(_ok_chat) as fake, _env({"KF": None}):
            target = _target(fake.url, key_file_env="KF")
            with self.assertRaises(ModelError) as ctx:
                chat(target, "hi")
        self.assertIn("KF", str(ctx.exception))

    def test_missing_key_file_raises_model_error(self) -> None:
        with FakeEndpoint(_ok_chat) as fake, _env(
            {"KF": "/nonexistent/path/key.txt"}
        ):
            target = _target(fake.url, key_file_env="KF")
            with self.assertRaises(ModelError) as ctx:
                chat(target, "hi")
        self.assertIn("KF", str(ctx.exception))

    def test_key_file_is_directory_raises_model_error(self) -> None:
        with FakeEndpoint(_ok_chat) as fake, _env({"KF": self._tmp_dir}):
            target = _target(fake.url, key_file_env="KF")
            with self.assertRaises(ModelError) as ctx:
                chat(target, "hi")
        self.assertIn("KF", str(ctx.exception))


class RedirectLeakTest(unittest.TestCase):
    """F1a: a redirecting endpoint must not walk the key to a new host."""

    def test_redirect_does_not_forward_key_to_new_host(self) -> None:
        def victim_respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "leaked"}}]}

        with FakeEndpoint(victim_respond) as victim:
            with FakeEndpoint(
                lambda _p, _b: {},
                status=302,
                headers={"Location": victim.url + "/chat/completions"},
            ) as redirector, _env({"K": "secret-key"}):
                target = _target(redirector.url, key_env="K")
                with self.assertRaises(ModelError):
                    chat(target, "hi")

            self.assertEqual(victim.requests, [])


class ProxyLeakTest(unittest.TestCase):
    """F1b: an env proxy must not be consulted for the endpoint call."""

    def test_proxy_env_var_is_not_consulted(self) -> None:
        with FakeEndpoint(_ok_chat) as fake, _env(
            {"http_proxy": "http://127.0.0.1:9"}
        ):
            result = chat(_target(fake.url), "hi")

        self.assertEqual(result, "answer text")


class ResponseShapeTest(unittest.TestCase):
    """F3: a 200 response missing the expected field raises ModelError
    naming that field, not a raw KeyError."""

    def test_chat_missing_choices_raises_model_error(self) -> None:
        def respond(_path: str, _body: dict) -> dict:
            return {"error": "boom"}

        with FakeEndpoint(respond) as fake:
            with self.assertRaises(ModelError) as ctx:
                chat(_target(fake.url), "hi")
        self.assertIn("choices", str(ctx.exception))

    def test_embed_missing_data_raises_model_error(self) -> None:
        def respond(_path: str, _body: dict) -> dict:
            return {"error": "boom"}

        with FakeEndpoint(respond) as fake:
            with self.assertRaises(ModelError) as ctx:
                embed(_target(fake.url), ["a"])
        self.assertIn("data", str(ctx.exception))


class EmbedIndexIntegrityTest(unittest.TestCase):
    """F4: a short or duplicated-index response must refuse, not return
    a silently misaligned vector list."""

    def test_short_response_raises_model_error(self) -> None:
        def respond(_path: str, _body: dict) -> dict:
            return {
                "data": [
                    {"index": 0, "embedding": [0.0]},
                    {"index": 1, "embedding": [1.0]},
                ]
            }

        with FakeEndpoint(respond) as fake:
            with self.assertRaises(ModelError):
                embed(_target(fake.url), ["a", "b", "c"])

    def test_duplicated_index_raises_model_error(self) -> None:
        def respond(_path: str, _body: dict) -> dict:
            return {
                "data": [
                    {"index": 0, "embedding": [0.0]},
                    {"index": 0, "embedding": [9.0]},
                ]
            }

        with FakeEndpoint(respond) as fake:
            with self.assertRaises(ModelError):
                embed(_target(fake.url), ["a", "b"])


class _ListHandler(logging.Handler):
    """Captures records into a list instead of writing anywhere, so a
    leak logged after stdout/stderr are redirected is still caught."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class NoLeakTest(unittest.TestCase):
    def _assert_no_leak(self, target: ModelTarget, secret: str) -> None:
        handler = _ListHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(ModelError) as ctx:
                chat(target, "hi")

        self.assertNotIn(secret, str(ctx.exception))
        self.assertNotIn(secret, out.getvalue())
        self.assertNotIn(secret, err.getvalue())

        formatted = "".join(
            traceback.format_exception(
                type(ctx.exception), ctx.exception, ctx.exception.__traceback__
            )
        )
        self.assertNotIn(secret, formatted)

        for record in handler.records:
            self.assertNotIn(secret, record.getMessage())

    def test_key_never_appears_in_output_or_exception(self) -> None:
        secret = "super-secret-value-shhh"

        def respond(_path: str, _body: dict) -> dict:
            return {"error": "boom"}

        with FakeEndpoint(respond, status=500) as fake, _env({"K": secret}):
            self._assert_no_leak(_target(fake.url, key_env="K"), secret)

    def test_key_never_appears_via_key_file(self) -> None:
        secret = "super-secret-file-value"
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir)
        key_path = Path(tmp_dir) / "key.txt"
        key_path.write_text(secret, encoding="utf-8")

        def respond(_path: str, _body: dict) -> dict:
            return {"error": "boom"}

        with FakeEndpoint(respond, status=500) as fake, _env({"KF": str(key_path)}):
            self._assert_no_leak(_target(fake.url, key_file_env="KF"), secret)


class StepIsConfiguredTest(unittest.TestCase):
    """agent-kb-yr0: `step_is_configured` answers presence only, and
    never raises, whatever shape `[models]` or the step's value take."""

    def test_step_present_is_true(self) -> None:
        self.assertTrue(step_is_configured({"models": {"embed": "m"}}, "embed"))

    def test_step_absent_is_false(self) -> None:
        self.assertFalse(step_is_configured({"models": {"summarize": "m"}}, "embed"))

    def test_models_table_absent_is_false(self) -> None:
        self.assertFalse(step_is_configured({}, "embed"))

    def test_models_not_a_table_is_false(self) -> None:
        self.assertFalse(step_is_configured({"models": "embed-model"}, "embed"))
        self.assertFalse(step_is_configured({"models": ["embed-model"]}, "embed"))

    def test_malformed_value_still_reads_as_configured(self) -> None:
        # The presence question and the validity question are separate
        # on purpose: a typo'd or malformed value must fail loudly in
        # resolve_target, never silently in step_is_configured.
        self.assertTrue(step_is_configured({"models": {"embed": 123}}, "embed"))
        self.assertTrue(step_is_configured({"models": {"embed": ""}}, "embed"))
        self.assertTrue(step_is_configured({"models": {"embed": None}}, "embed"))

    def test_legacy_endpoint_config_raises(self) -> None:
        # agent-kb-9i7: presence can never be answered past a stale
        # config; step_is_configured refuses before it looks at
        # [models] at all.
        config = {"endpoint": {"url": "https://api.example.com/v1"}}
        with self.assertRaises(ModelError) as ctx:
            step_is_configured(config, "embed")
        self.assertIn("[endpoint]", str(ctx.exception))
        self.assertIn("[providers]", str(ctx.exception))


class LegacyEndpointTest(unittest.TestCase):
    """agent-kb-9i7: no back-compat shim. A config still holding
    [endpoint] is refused loudly, naming the replacement, from both
    public entry points."""

    def test_resolve_target_refuses_naming_the_replacement(self) -> None:
        config = {
            "endpoint": {"url": "https://api.example.com/v1"},
            "models": {"summarize": "hosted:m"},
        }
        with self.assertRaises(ModelError) as ctx:
            resolve_target(config, "summarize")
        message = str(ctx.exception)
        self.assertIn("[endpoint]", message)
        self.assertIn("[providers.hosted]", message)
        self.assertIn('prefix every id under [models] with "hosted:"', message)


class SplitModelIdTest(unittest.TestCase):
    """agent-kb-9i7: every row of the split table `resolve_target`
    covers, each error naming the step."""

    PROVIDERS = {"hosted": {"url": "http://unused"}}

    def _resolve(self, value: object) -> ModelTarget:
        config = {"models": {"summarize": value}, "providers": self.PROVIDERS}
        return resolve_target(config, "summarize")

    def test_provider_and_model_split_on_first_colon(self) -> None:
        target = self._resolve("hosted:some-model")
        self.assertEqual((target.provider, target.model), ("hosted", "some-model"))

    def test_colon_inside_model_portion_survives(self) -> None:
        target = self._resolve("hosted:some-family:8b")
        self.assertEqual((target.provider, target.model), ("hosted", "some-family:8b"))

    def test_no_colon_raises_naming_the_step_and_missing_prefix(self) -> None:
        with self.assertRaises(ModelError) as ctx:
            self._resolve("some-model")
        message = str(ctx.exception)
        self.assertIn("[models].summarize", message)
        self.assertIn("no provider prefix", message)
        self.assertIn('"<provider>:some-model"', message)

    def test_empty_provider_raises_naming_the_step(self) -> None:
        with self.assertRaises(ModelError) as ctx:
            self._resolve(":some-model")
        message = str(ctx.exception)
        self.assertIn("[models].summarize", message)
        self.assertIn("empty provider name", message)

    def test_empty_model_raises_naming_the_step(self) -> None:
        with self.assertRaises(ModelError) as ctx:
            self._resolve("hosted:")
        message = str(ctx.exception)
        self.assertIn("[models].summarize", message)
        self.assertIn("names no model", message)

    def test_unknown_provider_raises_naming_the_step_and_provider(self) -> None:
        with self.assertRaises(ModelError) as ctx:
            self._resolve("nope:some-model")
        message = str(ctx.exception)
        self.assertIn("[models].summarize", message)
        self.assertIn("nope", message)
        self.assertIn("[providers.nope]", message)
        self.assertIn("not in config.toml", message)

    def test_non_string_value_raises_naming_the_step(self) -> None:
        for bad in (123, ["m"], {"nested": "m"}, 1.5, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ModelError) as ctx:
                    self._resolve(bad)
                message = str(ctx.exception)
                self.assertIn("[models].summarize", message)
                self.assertIn("not a string", message)

    def test_missing_step_raises_the_unchanged_missing_key_message(self) -> None:
        # vectors.status and vectors.search print this text verbatim;
        # it must stay byte-identical.
        with self.assertRaises(ModelError) as ctx:
            resolve_target({"models": {}, "providers": self.PROVIDERS}, "embed")
        self.assertEqual(str(ctx.exception), "missing [models].embed in config.toml")

    def test_models_table_absent_raises_missing_key_message(self) -> None:
        with self.assertRaises(ModelError) as ctx:
            resolve_target({}, "embed")
        self.assertEqual(str(ctx.exception), "missing [models].embed in config.toml")


class ProviderTableTest(unittest.TestCase):
    """agent-kb-9i7: a [providers.<name>] table is checked as strictly
    as [remotes]'s tables are: an unknown key is an error, and every
    recognized key is validated."""

    def _resolve(self, provider_table: dict) -> ModelTarget:
        config = {
            "models": {"summarize": "hosted:m"},
            "providers": {"hosted": provider_table},
        }
        return resolve_target(config, "summarize")

    def test_recognized_keys_all_come_through(self) -> None:
        target = self._resolve(
            {
                "url": "http://example.test",
                "key_env": "K",
                "key_file_env": "KF",
                "pdf_part": "image_url",
            }
        )
        self.assertEqual(target.url, "http://example.test")
        self.assertEqual(target.key_env, "K")
        self.assertEqual(target.key_file_env, "KF")
        self.assertEqual(target.pdf_part, "image_url")

    def test_pdf_part_defaults_to_file(self) -> None:
        target = self._resolve({"url": "http://example.test"})
        self.assertEqual(target.pdf_part, "file")

    def test_unrecognized_key_raises(self) -> None:
        with self.assertRaises(ModelError) as ctx:
            self._resolve({"url": "http://example.test", "vendor": "acme"})
        message = str(ctx.exception)
        self.assertIn("[providers.hosted]", message)
        self.assertIn("vendor", message)

    def test_missing_url_raises(self) -> None:
        with self.assertRaises(ModelError) as ctx:
            self._resolve({"key_env": "K"})
        self.assertIn("[providers.hosted]", str(ctx.exception))

    def test_provider_table_not_a_table_raises(self) -> None:
        config = {
            "models": {"summarize": "hosted:m"},
            "providers": {"hosted": "not-a-table"},
        }
        with self.assertRaises(ModelError) as ctx:
            resolve_target(config, "summarize")
        self.assertIn("[providers.hosted]", str(ctx.exception))

    def test_bad_pdf_part_raises(self) -> None:
        with self.assertRaises(ModelError) as ctx:
            self._resolve({"url": "http://example.test", "pdf_part": "carrier-pigeon"})
        message = str(ctx.exception)
        self.assertIn("[providers.hosted]", message)
        self.assertIn("pdf_part", message)
        self.assertIn("carrier-pigeon", message)


class TwoProviderTest(unittest.TestCase):
    """agent-kb-9i7: the reason this feature exists. Two [models] ids
    under two [providers] tables resolve to, and pay, two independent
    endpoints -- a summarize model on a hosted endpoint and an embed
    model on the machine under your desk are no longer mutually
    exclusive."""

    def test_summarize_and_embed_reach_different_providers(self) -> None:
        def respond_hosted(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "answer"}}]}

        def respond_desktop(_path: str, body: dict) -> dict:
            return {
                "data": [
                    {"index": i, "embedding": [1.0]}
                    for i in range(len(body["input"]))
                ]
            }

        with FakeEndpoint(respond_hosted) as hosted, FakeEndpoint(
            respond_desktop
        ) as desktop:
            config = {
                "models": {
                    "summarize": "hosted:chat-model",
                    "embed": "desktop:embed-model",
                },
                "providers": {
                    "hosted": {"url": hosted.url},
                    "desktop": {"url": desktop.url},
                },
            }
            text_target = resolve_target(config, "summarize")
            embed_target = resolve_target(config, "embed")

            chat(text_target, "hi")
            embed(embed_target, ["a", "b"])

        self.assertEqual(text_target.url, hosted.url)
        self.assertEqual(embed_target.url, desktop.url)
        self.assertEqual(len(hosted.requests), 1)
        self.assertEqual(hosted.requests[0].path, "/chat/completions")
        self.assertEqual(len(desktop.requests), 1)
        self.assertEqual(desktop.requests[0].path, "/embeddings")


if __name__ == "__main__":
    unittest.main()
