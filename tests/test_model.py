"""Chat and embed over the configured API endpoint, against the fake."""

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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from llmwiki.model import API_KEY_FILE_VAR, API_KEY_VAR, ModelError, chat, embed  # noqa: E402
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


def _ok_chat(_path: str, _body: dict) -> dict:
    return {"choices": [{"message": {"content": "answer text"}}]}


class ChatTest(unittest.TestCase):
    def test_request_shape_and_response(self) -> None:
        with FakeEndpoint(_ok_chat) as fake, _env(
            {API_KEY_VAR: "secret-key", API_KEY_FILE_VAR: None}
        ):
            config = {
                "endpoint": {"url": fake.url},
                "models": {"summarize": "chat-model"},
            }
            result = chat(config, "summarize", "hello there")

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
        with FakeEndpoint(_ok_chat) as fake, _env(
            {API_KEY_VAR: "k", API_KEY_FILE_VAR: None}
        ):
            config = {
                "endpoint": {"url": fake.url},
                "models": {"dedup": "judge-model"},
            }
            chat(config, "dedup", "pick one", temperature=0.0)

        self.assertEqual(
            fake.requests[0].body,
            {
                "model": "judge-model",
                "messages": [{"role": "user", "content": "pick one"}],
                "temperature": 0,
            },
        )

    def test_model_from_config_when_no_override(self) -> None:
        captured = {}

        def respond(_path: str, body: dict) -> dict:
            captured["model"] = body["model"]
            return {"choices": [{"message": {"content": "x"}}]}

        with FakeEndpoint(respond) as fake, _env(
            {API_KEY_VAR: "k", API_KEY_FILE_VAR: None}
        ):
            config = {
                "endpoint": {"url": fake.url},
                "models": {"summarize": "config-model"},
            }
            chat(config, "summarize", "hi")

        self.assertEqual(captured["model"], "config-model")

    def test_model_override_wins(self) -> None:
        captured = {}

        def respond(_path: str, body: dict) -> dict:
            captured["model"] = body["model"]
            return {"choices": [{"message": {"content": "x"}}]}

        with FakeEndpoint(respond) as fake, _env(
            {API_KEY_VAR: "k", API_KEY_FILE_VAR: None}
        ):
            config = {
                "endpoint": {"url": fake.url},
                "models": {"summarize": "config-model"},
            }
            chat(config, "summarize", "hi", model="override-model")

        self.assertEqual(captured["model"], "override-model")

    def test_missing_endpoint_url_raises(self) -> None:
        with _env({API_KEY_VAR: "k", API_KEY_FILE_VAR: None}):
            with self.assertRaises(ModelError) as ctx:
                chat({"models": {"summarize": "m"}}, "summarize", "hi")
        message = str(ctx.exception)
        self.assertIn("endpoint", message)
        self.assertIn("url", message)

    def test_missing_model_step_raises(self) -> None:
        with _env({API_KEY_VAR: "k", API_KEY_FILE_VAR: None}):
            with self.assertRaises(ModelError) as ctx:
                config = {"endpoint": {"url": "http://unused"}, "models": {}}
                chat(config, "summarize", "hi")
        self.assertIn("summarize", str(ctx.exception))


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

        with FakeEndpoint(respond) as fake, _env(
            {API_KEY_VAR: "k", API_KEY_FILE_VAR: None}
        ):
            config = {
                "endpoint": {"url": fake.url},
                "models": {"embed": "embed-model"},
            }
            vectors = embed(config, ["a", "b", "c"])

        self.assertEqual(vectors, [[0.0], [1.0], [2.0]])

    def test_uses_models_embed_key_and_path(self) -> None:
        captured = {}

        def respond(path: str, body: dict) -> dict:
            captured["model"] = body["model"]
            captured["path"] = path
            return {"data": [{"index": 0, "embedding": [1.0]}]}

        with FakeEndpoint(respond) as fake, _env(
            {API_KEY_VAR: "k", API_KEY_FILE_VAR: None}
        ):
            config = {
                "endpoint": {"url": fake.url},
                "models": {"embed": "embed-model"},
            }
            embed(config, ["only"])

        self.assertEqual(captured["model"], "embed-model")
        self.assertEqual(captured["path"], "/embeddings")

    def test_embed_model_override(self) -> None:
        captured = {}

        def respond(_path: str, body: dict) -> dict:
            captured["model"] = body["model"]
            return {"data": [{"index": 0, "embedding": [1.0]}]}

        with FakeEndpoint(respond) as fake, _env(
            {API_KEY_VAR: "k", API_KEY_FILE_VAR: None}
        ):
            config = {
                "endpoint": {"url": fake.url},
                "models": {"embed": "embed-model"},
            }
            embed(config, ["only"], model="override")

        self.assertEqual(captured["model"], "override")


class CredentialTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp_dir)

    def _write_key_file(self, content: str) -> Path:
        path = Path(self._tmp_dir) / "key.txt"
        path.write_text(content, encoding="utf-8")
        return path

    def _config(self, fake: FakeEndpoint) -> dict:
        return {"endpoint": {"url": fake.url}, "models": {"summarize": "m"}}

    def test_value_wins_when_both_set(self) -> None:
        key_path = self._write_key_file("file-key\n")

        with FakeEndpoint(_ok_chat) as fake, _env(
            {API_KEY_VAR: "value-key", API_KEY_FILE_VAR: str(key_path)}
        ):
            chat(self._config(fake), "summarize", "hi")

        self.assertEqual(
            fake.requests[0].headers.get("Authorization"), "Bearer value-key"
        )

    def test_file_used_when_only_file_set_strips_one_newline(self) -> None:
        key_path = self._write_key_file("file-key\n")

        with FakeEndpoint(_ok_chat) as fake, _env(
            {API_KEY_VAR: None, API_KEY_FILE_VAR: str(key_path)}
        ):
            chat(self._config(fake), "summarize", "hi")

        self.assertEqual(
            fake.requests[0].headers.get("Authorization"), "Bearer file-key"
        )

    def test_neither_set_raises_naming_both_vars(self) -> None:
        with FakeEndpoint(_ok_chat) as fake, _env(
            {API_KEY_VAR: None, API_KEY_FILE_VAR: None}
        ):
            with self.assertRaises(ModelError) as ctx:
                chat(self._config(fake), "summarize", "hi")

        message = str(ctx.exception)
        self.assertIn(API_KEY_VAR, message)
        self.assertIn(API_KEY_FILE_VAR, message)


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
            ) as redirector, _env(
                {API_KEY_VAR: "secret-key", API_KEY_FILE_VAR: None}
            ):
                config = {
                    "endpoint": {"url": redirector.url},
                    "models": {"summarize": "m"},
                }
                with self.assertRaises(ModelError):
                    chat(config, "summarize", "hi")

            self.assertEqual(victim.requests, [])


class ProxyLeakTest(unittest.TestCase):
    """F1b: an env proxy must not be consulted for the endpoint call."""

    def test_proxy_env_var_is_not_consulted(self) -> None:
        with FakeEndpoint(_ok_chat) as fake, _env(
            {
                API_KEY_VAR: "k",
                API_KEY_FILE_VAR: None,
                "http_proxy": "http://127.0.0.1:9",
            }
        ):
            config = {"endpoint": {"url": fake.url}, "models": {"summarize": "m"}}
            result = chat(config, "summarize", "hi")

        self.assertEqual(result, "answer text")


class ApiKeyFileErrorTest(unittest.TestCase):
    """F2: a bad LLM_WIKI_API_KEY_FILE raises ModelError, not a raw
    OSError/ValueError subtype."""

    def test_missing_key_file_raises_model_error(self) -> None:
        with FakeEndpoint(_ok_chat) as fake, _env(
            {API_KEY_VAR: None, API_KEY_FILE_VAR: "/nonexistent/path/key.txt"}
        ):
            config = {"endpoint": {"url": fake.url}, "models": {"summarize": "m"}}
            with self.assertRaises(ModelError) as ctx:
                chat(config, "summarize", "hi")
        self.assertIn(API_KEY_FILE_VAR, str(ctx.exception))

    def test_key_file_is_directory_raises_model_error(self) -> None:
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir)
        with FakeEndpoint(_ok_chat) as fake, _env(
            {API_KEY_VAR: None, API_KEY_FILE_VAR: tmp_dir}
        ):
            config = {"endpoint": {"url": fake.url}, "models": {"summarize": "m"}}
            with self.assertRaises(ModelError) as ctx:
                chat(config, "summarize", "hi")
        self.assertIn(API_KEY_FILE_VAR, str(ctx.exception))


class ResponseShapeTest(unittest.TestCase):
    """F3: a 200 response missing the expected field raises ModelError
    naming that field, not a raw KeyError."""

    def test_chat_missing_choices_raises_model_error(self) -> None:
        def respond(_path: str, _body: dict) -> dict:
            return {"error": "boom"}

        with FakeEndpoint(respond) as fake, _env(
            {API_KEY_VAR: "k", API_KEY_FILE_VAR: None}
        ):
            config = {"endpoint": {"url": fake.url}, "models": {"summarize": "m"}}
            with self.assertRaises(ModelError) as ctx:
                chat(config, "summarize", "hi")
        self.assertIn("choices", str(ctx.exception))

    def test_embed_missing_data_raises_model_error(self) -> None:
        def respond(_path: str, _body: dict) -> dict:
            return {"error": "boom"}

        with FakeEndpoint(respond) as fake, _env(
            {API_KEY_VAR: "k", API_KEY_FILE_VAR: None}
        ):
            config = {"endpoint": {"url": fake.url}, "models": {"embed": "m"}}
            with self.assertRaises(ModelError) as ctx:
                embed(config, ["a"])
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

        with FakeEndpoint(respond) as fake, _env(
            {API_KEY_VAR: "k", API_KEY_FILE_VAR: None}
        ):
            config = {"endpoint": {"url": fake.url}, "models": {"embed": "m"}}
            with self.assertRaises(ModelError):
                embed(config, ["a", "b", "c"])

    def test_duplicated_index_raises_model_error(self) -> None:
        def respond(_path: str, _body: dict) -> dict:
            return {
                "data": [
                    {"index": 0, "embedding": [0.0]},
                    {"index": 0, "embedding": [9.0]},
                ]
            }

        with FakeEndpoint(respond) as fake, _env(
            {API_KEY_VAR: "k", API_KEY_FILE_VAR: None}
        ):
            config = {"endpoint": {"url": fake.url}, "models": {"embed": "m"}}
            with self.assertRaises(ModelError):
                embed(config, ["a", "b"])


class _ListHandler(logging.Handler):
    """Captures records into a list instead of writing anywhere, so a
    leak logged after stdout/stderr are redirected is still caught."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class NoLeakTest(unittest.TestCase):
    def _assert_no_leak(self, config: dict, secret: str) -> None:
        handler = _ListHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(ModelError) as ctx:
                chat(config, "summarize", "hi")

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

        with FakeEndpoint(respond, status=500) as fake, _env(
            {API_KEY_VAR: secret, API_KEY_FILE_VAR: None}
        ):
            config = {"endpoint": {"url": fake.url}, "models": {"summarize": "m"}}
            self._assert_no_leak(config, secret)

    def test_key_never_appears_via_key_file(self) -> None:
        secret = "super-secret-file-value"
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir)
        key_path = Path(tmp_dir) / "key.txt"
        key_path.write_text(secret, encoding="utf-8")

        def respond(_path: str, _body: dict) -> dict:
            return {"error": "boom"}

        with FakeEndpoint(respond, status=500) as fake, _env(
            {API_KEY_VAR: None, API_KEY_FILE_VAR: str(key_path)}
        ):
            config = {"endpoint": {"url": fake.url}, "models": {"summarize": "m"}}
            self._assert_no_leak(config, secret)


if __name__ == "__main__":
    unittest.main()
