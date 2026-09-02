"""The remotes client: the parse, the token, the transport, and the
rule that two wikis' answers never merge."""

import json
import os
import socket
import sys
import threading
import tomllib
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from llmwiki import remotes  # noqa: E402
from llmwiki.model import API_KEY_VAR  # noqa: E402
from fake_wiki import FakeWiki, Reply  # noqa: E402

TOKEN_VAR = "HOMELAB_KB_TOKEN"
TOKEN = "token-value-nothing-may-print"


def _hits_body(*titles: str) -> bytes:
    """A search body the route would send, scores included: the client
    parses them and drops them."""
    hits = [
        {
            "score": 0.9 - index / 100,
            "name": f"{title.lower()}.md",
            "title": title,
            "updated": "2026-01-01T00:00:00Z",
            "size": 100 + index,
        }
        for index, title in enumerate(titles)
    ]
    return json.dumps({"hits": hits}).encode("utf-8")


def _dead_port() -> int:
    """A port with nothing listening: bound, read, and closed."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _config(toml: str) -> dict:
    return tomllib.loads(toml)


class ParseRemotesTest(unittest.TestCase):
    """Every refusal names the remote and opens no socket. The whole
    class runs without a server, which is the point."""

    def _refuses(self, toml: str, *fragments: str) -> str:
        with self.assertRaises(ValueError) as caught:
            remotes.parse_remotes(_config(toml))
        message = str(caught.exception)
        for fragment in fragments:
            self.assertIn(fragment, message)
        return message

    def test_a_good_table_parses(self) -> None:
        table = remotes.parse_remotes(
            _config(
                '[remotes.homelab]\n'
                'url = "https://kb.example.net/kb/homelab"\n'
                'token_env = "HOMELAB_KB_TOKEN"\n'
                '\n[remotes.reports]\n'
                'url = "https://kb.example.net/kb/reports"\n'
            )
        )
        self.assertEqual(list(table), ["homelab", "reports"])
        self.assertEqual(
            table["homelab"],
            remotes.Remote(
                "homelab",
                "https://kb.example.net/kb/homelab",
                "HOMELAB_KB_TOKEN",
            ),
        )
        self.assertIsNone(table["reports"].token_env)

    def test_no_remotes_table_is_an_empty_dict(self) -> None:
        self.assertEqual(remotes.parse_remotes({}), {})

    def test_a_trailing_slash_is_stripped(self) -> None:
        table = remotes.parse_remotes(
            _config('[remotes.a]\nurl = "https://h.example/kb/a/"\n')
        )
        self.assertEqual(table["a"].url, "https://h.example/kb/a")

    def test_mode_read_is_accepted_and_stored_nowhere(self) -> None:
        table = remotes.parse_remotes(
            _config(
                '[remotes.a]\nurl = "https://h.example/kb/a"\n'
                'mode = "read"\n'
            )
        )
        self.assertEqual(table["a"], remotes.Remote("a", "https://h.example/kb/a", None))
        self.assertNotIn("mode", remotes.Remote._fields)

    def test_a_missing_url_is_refused(self) -> None:
        self._refuses('[remotes.a]\ntoken_env = "T"\n', "remote a", "url")

    def test_http_is_refused(self) -> None:
        self._refuses(
            '[remotes.a]\nurl = "http://h.example/kb/a"\n', "remote a", "https"
        )

    def test_userinfo_in_the_url_is_refused(self) -> None:
        message = self._refuses(
            '[remotes.a]\nurl = "https://user:secret@h.example/kb/a"\n',
            "remote a",
            "userinfo",
        )
        self.assertNotIn("secret", message)

    def test_a_query_in_the_url_is_refused(self) -> None:
        self._refuses(
            '[remotes.a]\nurl = "https://h.example/kb/a?x=1"\n',
            "remote a",
            "query",
        )

    def test_a_fragment_in_the_url_is_refused(self) -> None:
        self._refuses(
            '[remotes.a]\nurl = "https://h.example/kb/a#top"\n',
            "remote a",
            "fragment",
        )

    def test_a_url_with_no_host_is_refused(self) -> None:
        self._refuses('[remotes.a]\nurl = "https:///kb/a"\n', "remote a", "host")

    def test_a_url_that_is_not_a_string_is_refused(self) -> None:
        self._refuses("[remotes.a]\nurl = 7\n", "remote a", "url")

    def test_an_unknown_key_is_refused_by_name(self) -> None:
        self._refuses(
            '[remotes.a]\nurl = "https://h.example/kb/a"\n'
            'token-env = "T"\n',
            "remote a",
            "token-env",
        )

    def test_mode_write_is_refused(self) -> None:
        self._refuses(
            '[remotes.a]\nurl = "https://h.example/kb/a"\nmode = "write"\n',
            "remote a",
            "mode",
        )

    def test_mode_admin_is_refused(self) -> None:
        self._refuses(
            '[remotes.a]\nurl = "https://h.example/kb/a"\nmode = "admin"\n',
            "remote a",
            "mode",
        )

    def test_a_token_env_that_is_not_a_string_is_refused(self) -> None:
        self._refuses(
            '[remotes.a]\nurl = "https://h.example/kb/a"\ntoken_env = 7\n',
            "remote a",
            "token_env",
        )

    def test_the_label_local_is_refused(self) -> None:
        self._refuses(
            '[remotes.local]\nurl = "https://h.example/kb/a"\n',
            "remote local",
            "reserved",
        )

    def test_a_label_holding_a_tab_is_refused(self) -> None:
        self._refuses(
            '[remotes."a\\tb"]\nurl = "https://h.example/kb/a"\n', "control"
        )

    def test_a_label_holding_a_space_is_refused(self) -> None:
        self._refuses(
            '[remotes."a b"]\nurl = "https://h.example/kb/a"\n', "space"
        )

    def test_a_remotes_entry_that_is_not_a_table_is_refused(self) -> None:
        self._refuses('[remotes]\na = "https://h.example/kb/a"\n', "remote a", "table")

    def test_a_remotes_value_that_is_not_a_table_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            remotes.parse_remotes({"remotes": ["a"]})
        self.assertIn("[remotes] is not a table", str(caught.exception))


class SeparationTest(unittest.TestCase):
    def test_a_hit_carries_no_score(self) -> None:
        """A later reader who adds the field back fails here first."""
        self.assertNotIn("score", remotes.RemoteHit._fields)
        self.assertEqual(
            remotes.RemoteHit._fields,
            ("rank", "name", "title", "updated", "size"),
        )


class SearchOneTest(unittest.TestCase):
    """Request building, the token, the response parse, and the cap.
    Every case runs against a real socket on loopback."""

    def _wiki(self, respond) -> FakeWiki:
        wiki = FakeWiki(respond)
        self.addCleanup(wiki.close)
        return wiki

    def _remote(self, wiki: FakeWiki, token_env=None) -> remotes.Remote:
        return remotes.Remote("homelab", wiki.url + "/kb/homelab", token_env)

    def _answering(self, reply: Reply) -> FakeWiki:
        return self._wiki(lambda _request: reply)

    def test_the_request_is_a_get_carrying_q_n_and_kind(self) -> None:
        wiki = self._answering(Reply(body=_hits_body("North")))
        remotes.search_one(self._remote(wiki), "cold places", 5, "summary")
        (request,) = wiki.requests
        self.assertEqual(request.path, "/kb/homelab/search")
        self.assertEqual(
            request.query,
            {"q": ["cold places"], "n": ["5"], "kind": ["summary"]},
        )

    def test_no_kind_sends_no_kind_parameter(self) -> None:
        wiki = self._answering(Reply(body=_hits_body("North")))
        remotes.search_one(self._remote(wiki), "cold", 10, None)
        self.assertNotIn("kind", wiki.requests[0].query)

    def test_hits_come_back_ranked_from_one_with_no_score(self) -> None:
        wiki = self._answering(Reply(body=_hits_body("North", "South")))
        answer = remotes.search_one(self._remote(wiki), "cold", 10, None)
        self.assertEqual(answer.remote, "homelab")
        self.assertEqual([hit.rank for hit in answer.hits], [1, 2])
        self.assertEqual(
            answer.hits[0],
            remotes.RemoteHit(1, "north.md", "North", "2026-01-01T00:00:00Z", 100),
        )

    def test_an_unknown_field_on_the_wire_is_ignored(self) -> None:
        body = json.dumps(
            {
                "hits": [
                    {
                        "name": "north.md",
                        "title": "North",
                        "updated": "2026-01-01T00:00:00Z",
                        "size": 4,
                        "future_field": {"anything": 1},
                    }
                ],
                "total": 1,
            }
        ).encode("utf-8")
        wiki = self._answering(Reply(body=body))
        answer = remotes.search_one(self._remote(wiki), "cold", 10, None)
        self.assertEqual(answer.hits[0].name, "north.md")

    def test_a_title_holding_a_tab_is_flattened(self) -> None:
        body = json.dumps(
            {
                "hits": [
                    {
                        "name": "north.md",
                        "title": "North\tforged\tcolumn",
                        "updated": "2026-01-01T00:00:00Z",
                        "size": 4,
                    }
                ]
            }
        ).encode("utf-8")
        wiki = self._answering(Reply(body=body))
        answer = remotes.search_one(self._remote(wiki), "cold", 10, None)
        self.assertEqual(answer.hits[0].title, "North forged column")

    def test_a_token_reaches_the_configured_host_once(self) -> None:
        wiki = self._answering(Reply(body=_hits_body("North")))
        with unittest.mock.patch.dict(os.environ, {TOKEN_VAR: TOKEN}):
            answer = remotes.search_one(
                self._remote(wiki, TOKEN_VAR), "cold", 10, None
            )
        self.assertIsInstance(answer, remotes.RemoteRanking)
        (request,) = wiki.requests
        self.assertEqual(
            request.headers.get("Authorization"), f"Bearer {TOKEN}"
        )

    def test_no_token_env_sends_no_authorization_header(self) -> None:
        wiki = self._answering(Reply(body=_hits_body("North")))
        remotes.search_one(self._remote(wiki), "cold", 10, None)
        self.assertIsNone(wiki.requests[0].headers.get("Authorization"))

    def test_an_absent_variable_refuses_before_the_request(self) -> None:
        wiki = self._answering(Reply(body=_hits_body("North")))
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(TOKEN_VAR, None)
            answer = remotes.search_one(
                self._remote(wiki, TOKEN_VAR), "cold", 10, None
            )
        self.assertEqual(answer, remotes.RemoteFailure("homelab", "no_token"))
        self.assertEqual(wiki.requests, [])

    def test_an_empty_variable_refuses_before_the_request(self) -> None:
        wiki = self._answering(Reply(body=_hits_body("North")))
        with unittest.mock.patch.dict(os.environ, {TOKEN_VAR: ""}):
            answer = remotes.search_one(
                self._remote(wiki, TOKEN_VAR), "cold", 10, None
            )
        self.assertEqual(answer, remotes.RemoteFailure("homelab", "no_token"))
        self.assertEqual(wiki.requests, [])

    def test_the_model_key_never_reaches_a_wiki(self) -> None:
        wiki = self._answering(Reply(body=_hits_body("North")))
        env = {API_KEY_VAR: "model-key-value", TOKEN_VAR: TOKEN}
        with unittest.mock.patch.dict(os.environ, env):
            remotes.search_one(self._remote(wiki, TOKEN_VAR), "cold", 10, None)
        sent = str(wiki.requests[0].headers)
        self.assertIn(TOKEN, sent)
        self.assertNotIn("model-key-value", sent)

    def test_a_failure_never_carries_the_token(self) -> None:
        wiki = self._answering(Reply(status=401, body=b'{"error": "x"}'))
        with unittest.mock.patch.dict(os.environ, {TOKEN_VAR: TOKEN}):
            answer = remotes.search_one(
                self._remote(wiki, TOKEN_VAR), "cold", 10, None
            )
        self.assertEqual(answer.code, "unauthorized")
        self.assertNotIn(TOKEN, repr(answer))

    def test_a_redirect_is_http_error_and_the_second_host_sees_nothing(self):
        """The property `model.OPENER` exists for: a token must not
        follow a 3xx to a host `config.toml` never named."""
        second = self._answering(Reply(body=_hits_body("Elsewhere")))
        first = self._wiki(
            lambda _request: Reply(
                status=302,
                headers={"Location": second.url + "/kb/homelab/search"},
            )
        )
        with unittest.mock.patch.dict(os.environ, {TOKEN_VAR: TOKEN}):
            answer = remotes.search_one(
                self._remote(first, TOKEN_VAR), "cold", 10, None
            )
        self.assertEqual(
            answer, remotes.RemoteFailure("homelab", "http_error")
        )
        self.assertEqual(len(first.requests), 1)
        self.assertEqual(second.requests, [])

    def test_each_status_maps_to_its_own_code(self) -> None:
        expected = {
            401: "unauthorized",
            403: "unauthorized",
            404: "not_found",
            429: "rate_limited",
            501: "no_embed_model",
            502: "upstream_model_failed",
            503: "index_stale",
            418: "http_error",
            500: "http_error",
        }
        for status, code in expected.items():
            with self.subTest(status=status):
                wiki = self._answering(Reply(status=status, body=b"{}"))
                answer = remotes.search_one(
                    self._remote(wiki), "cold", 10, None
                )
                self.assertEqual(
                    answer, remotes.RemoteFailure("homelab", code)
                )
                self.assertEqual(len(wiki.requests), 1, "a retry sends two")

    def test_a_dead_proxy_in_the_environment_is_ignored(self) -> None:
        """`model.OPENER` is built with `ProxyHandler({})`, so a token
        never travels through a proxy the environment named and the
        config did not."""
        wiki = self._answering(Reply(body=_hits_body("North")))
        dead = f"http://127.0.0.1:{_dead_port()}"
        with unittest.mock.patch.dict(
            os.environ, {"http_proxy": dead, "https_proxy": dead}
        ):
            answer = remotes.search_one(self._remote(wiki), "cold", 10, None)
        self.assertIsInstance(answer, remotes.RemoteRanking)
        self.assertEqual(len(wiki.requests), 1)

    def test_a_closed_port_is_unreachable(self) -> None:
        remote = remotes.Remote(
            "homelab", f"http://127.0.0.1:{_dead_port()}/kb/homelab", None
        )
        answer = remotes.search_one(remote, "cold", 10, None)
        self.assertEqual(answer, remotes.RemoteFailure("homelab", "unreachable"))

    def test_a_slow_remote_times_out(self) -> None:
        held = threading.Event()
        self.addCleanup(held.set)

        def respond(_request):
            held.wait(10)
            return Reply(body=_hits_body("North"))

        wiki = self._wiki(respond)
        with unittest.mock.patch.object(remotes, "REMOTE_TIMEOUT_SEC", 0.3):
            answer = remotes.search_one(self._remote(wiki), "cold", 10, None)
        self.assertEqual(answer, remotes.RemoteFailure("homelab", "timeout"))

    def test_a_body_over_the_cap_is_refused_whole(self) -> None:
        """No `Content-Length` at all, so nothing but reading one byte
        past the cap catches the size."""
        # The first MAX_RESPONSE_BYTES are valid JSON on their own, so
        # only the cap can refuse this: a truncating parse would not.
        oversize = b'{"hits": []}' + b" " * remotes.MAX_RESPONSE_BYTES
        wiki = self._answering(Reply(body=oversize, length_header=False))
        answer = remotes.search_one(self._remote(wiki), "cold", 10, None)
        self.assertEqual(
            answer, remotes.RemoteFailure("homelab", "bad_response")
        )

    def test_an_understated_content_length_truncates_the_body(self) -> None:
        """A `Content-Length` smaller than the body delimits the read,
        so the client sees a truncated JSON document and refuses the
        whole response rather than salvaging the prefix."""
        body = b'{"hits": [], "pad": "' + b"x" * 5000 + b'"}'
        wiki = self._answering(
            Reply(body=body, headers={"Content-Length": "20"})
        )
        answer = remotes.search_one(self._remote(wiki), "cold", 10, None)
        self.assertEqual(
            answer, remotes.RemoteFailure("homelab", "bad_response")
        )

    def test_garbage_bodies_are_refused_whole(self) -> None:
        good = {
            "name": "north.md",
            "title": "North",
            "updated": "2026-01-01T00:00:00Z",
            "size": 4,
        }
        bodies = {
            "not json": b"<html>nope</html>",
            "hits not a list": b'{"hits": {"a": 1}}',
            "hits absent": b"{}",
            "body not an object": b"[]",
            "hit not an object": b'{"hits": ["north.md"]}',
            "no title": json.dumps(
                {"hits": [{k: v for k, v in good.items() if k != "title"}]}
            ).encode(),
            "size not an int": json.dumps(
                {"hits": [{**good, "size": "4"}]}
            ).encode(),
            "name walks up": json.dumps(
                {"hits": [{**good, "name": "../config.toml"}]}
            ).encode(),
            "name holds a tab": json.dumps(
                {"hits": [{**good, "name": "no\trth.md"}]}
            ).encode(),
            "name holds a separator": json.dumps(
                {"hits": [{**good, "name": "sub/north.md"}]}
            ).encode(),
            "name is absolute": json.dumps(
                {"hits": [{**good, "name": "/etc/north.md"}]}
            ).encode(),
            "name is not markdown": json.dumps(
                {"hits": [{**good, "name": "north.txt"}]}
            ).encode(),
        }
        for label, body in bodies.items():
            with self.subTest(body=label):
                wiki = self._answering(Reply(body=body))
                answer = remotes.search_one(
                    self._remote(wiki), "cold", 10, None
                )
                self.assertEqual(
                    answer, remotes.RemoteFailure("homelab", "bad_response")
                )


class RemotePageTest(unittest.TestCase):
    def _wiki(self, respond) -> FakeWiki:
        wiki = FakeWiki(respond)
        self.addCleanup(wiki.close)
        return wiki

    def test_bytes_come_back_unchanged_including_invalid_utf8(self) -> None:
        raw = b"# North\n\xff\xfe not utf-8\n"
        wiki = self._wiki(
            lambda _r: Reply(body=raw, content_type="text/markdown")
        )
        remote = remotes.Remote("homelab", wiki.url + "/kb/homelab", None)
        self.assertEqual(remotes.page(remote, "north.md"), raw)
        self.assertEqual(wiki.requests[0].path, "/kb/homelab/page/north.md")

    def test_a_name_holding_a_separator_is_quoted_whole(self) -> None:
        wiki = self._wiki(
            lambda _r: Reply(status=404, body=b'{"error": "not_found"}')
        )
        remote = remotes.Remote("homelab", wiki.url + "/kb/homelab", None)
        with self.assertRaises(remotes.RemoteError) as caught:
            remotes.page(remote, "../config.toml")
        self.assertEqual(caught.exception.code, "not_found")
        self.assertEqual(
            wiki.requests[0].path, "/kb/homelab/page/..%2Fconfig.toml"
        )

    def test_a_media_type_parameter_is_accepted(self) -> None:
        wiki = self._wiki(
            lambda _r: Reply(
                body=b"# North\n",
                content_type="Text/Markdown; charset=utf-8",
            )
        )
        remote = remotes.Remote("homelab", wiki.url + "/kb/homelab", None)
        self.assertEqual(remotes.page(remote, "north.md"), b"# North\n")

    def test_html_is_refused(self) -> None:
        wiki = self._wiki(
            lambda _r: Reply(body=b"<html>", content_type="text/html")
        )
        remote = remotes.Remote("homelab", wiki.url + "/kb/homelab", None)
        with self.assertRaises(remotes.RemoteError) as caught:
            remotes.page(remote, "north.md")
        self.assertEqual(caught.exception.code, "bad_response")

    def test_a_page_over_the_cap_is_refused_whole(self) -> None:
        oversize = b"x" * (remotes.MAX_RESPONSE_BYTES + 1)
        wiki = self._wiki(
            lambda _r: Reply(
                body=oversize,
                content_type="text/markdown",
                length_header=False,
            )
        )
        remote = remotes.Remote("homelab", wiki.url + "/kb/homelab", None)
        with self.assertRaises(remotes.RemoteError) as caught:
            remotes.page(remote, "north.md")
        self.assertEqual(caught.exception.code, "bad_response")


if __name__ == "__main__":
    unittest.main()
