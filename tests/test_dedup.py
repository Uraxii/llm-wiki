"""Dedup: story membership and the push warning, against a hand-built
kb and the fake endpoint for the judge."""

import hashlib
import io
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from llmwiki.core import parse_frontmatter, render_frontmatter  # noqa: E402
from llmwiki import cli, dedup  # noqa: E402
from llmwiki.dedup import DEDUP_PROMPT  # noqa: E402
from fake_endpoint import FakeEndpoint  # noqa: E402
from kb_config import config_toml  # noqa: E402

DEFAULT_CONFIG = config_toml("http://unused", {"summarize": "cheap"}, extra="[identifiers.tag]\n")


def _run_quiet(root, digests=None):
    """`dedup.run`, with its `N planned` print captured instead of
    buried in the unittest summary; returns `(code, stdout)`."""
    with redirect_stdout(io.StringIO()) as buf:
        code = dedup.run(root, digests)
    return code, buf.getvalue()


def _rebuild_quiet(root):
    """`dedup.rebuild`, mirroring `_run_quiet`."""
    with redirect_stdout(io.StringIO()) as buf:
        code = dedup.rebuild(root)
    return code, buf.getvalue()


class DedupTest(unittest.TestCase):
    """A from-scratch kb per test: dirs, log, a default identifier
    vocabulary. Page file names are deliberately unrelated to their
    titles, so a title's slug never collides with a fixture file by
    accident."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / ".kb"
        for sub in ("wiki", "sources"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "log.md").write_text("# log\n")
        self._write_config(DEFAULT_CONFIG)

    def _write_config(self, text: str) -> None:
        (self.root / "config.toml").write_text(text)

    def _digest(self, text: str) -> str:
        """A source hash with a byte file under sources/, so the
        summary that claims it never trips the dangling-source check."""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        (self.root / "sources" / f"{digest}.md").write_text(text)
        return digest

    def _write_summary(self, name, digest, title, identifiers, fetched=None):
        fields = {"kind": "summary", "title": title, "source": digest}
        if fetched is not None:
            fields["fetched"] = fetched
        fields["identifiers"] = identifiers
        path = self.root / "wiki" / f"{name}.md"
        path.write_text(render_frontmatter(fields, f"Abstract for {title}."))
        return path

    def _write_story(self, name, title, members, identifiers):
        fields = {
            "kind": "story",
            "title": title,
            "members": members,
            "identifiers": identifiers,
        }
        path = self.root / "wiki" / f"{name}.md"
        path.write_text(render_frontmatter(fields, "placeholder body"))
        return path

    def _write_agent(self, name, title, identifiers, kind="host"):
        fields = {"kind": kind, "title": title, "identifiers": identifiers}
        path = self.root / "wiki" / f"{name}.md"
        path.write_text(render_frontmatter(fields, "agent page body"))
        return path

    def _fields(self, name: str) -> dict:
        text = (self.root / "wiki" / f"{name}.md").read_text(encoding="utf-8")
        return parse_frontmatter(text)[0]

    # -- join -----------------------------------------------------------

    def test_join_across_casing_and_whitespace(self) -> None:
        self._write_story("story-file", "Kettle Story", [], ["tag:Example  Thing"])
        digest = self._digest("kettle source text")
        self._write_summary("summary-file", digest, "Kettle Summary", ["tag:  example thing  "])

        code, out = _run_quiet(self.root)

        self.assertEqual(code, 0)
        self.assertIn("dedup: 1 planned", out)
        self.assertEqual(self._fields("story-file")["members"], [digest])
        self.assertEqual(self._fields("summary-file")["story"], "story-file")
        self.assertIn(f"{digest} -> story-file (joined)", (self.root / "log.md").read_text())

    def test_no_shared_identifier_gives_new_story(self) -> None:
        self._write_story("other-story", "Other Story", [], ["tag:something-else"])
        digest = self._digest("solo source text")
        self._write_summary("solo-file", digest, "Solo Widget", ["tag:unrelated"])

        code, _out = _run_quiet(self.root)

        self.assertEqual(code, 0)
        story_path = self.root / "wiki" / "solo-widget.md"
        self.assertTrue(story_path.is_file())
        fields, body = parse_frontmatter(story_path.read_text())
        self.assertEqual(fields["members"], [digest])
        self.assertIn("Solo Widget", body)
        self.assertEqual(self._fields("solo-file")["story"], "solo-widget")
        # the unrelated existing story is untouched
        self.assertEqual(self._fields("other-story")["members"], [])

    def test_no_identifiers_makes_singleton_and_no_model_call(self) -> None:
        digest = self._digest("lonely source text")
        self._write_summary("lonely-file", digest, "Lonely Widget", [])

        def respond(_path, _body):
            raise AssertionError("dedup must not call the model with zero candidates")

        with FakeEndpoint(respond) as fake:
            self._write_config(
                config_toml(
                    fake.url,
                    {"summarize": "cheap", "dedup": "judge"},
                    extra="[identifiers.tag]\n",
                )
            )
            code, _out = _run_quiet(self.root)

        self.assertEqual(code, 0)
        self.assertEqual(fake.requests, [])
        self.assertTrue((self.root / "wiki" / "lonely-widget.md").is_file())

    def test_none_reply_gives_new_story(self) -> None:
        self._write_story("candidate-story", "Candidate Story", ["deadbeef01"], ["tag:shared"])
        digest = self._digest("none reply source text")
        self._write_summary("summary-file", digest, "Fresh Gadget", ["tag:shared"])

        def respond(_path, _body):
            return {"choices": [{"message": {"content": "NONE\n"}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(
                config_toml(
                    fake.url,
                    {"summarize": "cheap", "dedup": "judge"},
                    extra="[identifiers.tag]\n",
                )
            )
            code, _out = _run_quiet(self.root)

        self.assertEqual(code, 0)
        self.assertEqual(len(fake.requests), 1)
        self.assertTrue((self.root / "wiki" / "fresh-gadget.md").is_file())
        self.assertEqual(self._fields("candidate-story")["members"], ["deadbeef01"])

    def test_judge_call_pins_temperature_zero(self) -> None:
        """The judge is a classifier under a one-line reply contract,
        and sampling it made the same frozen case answer NONE, join,
        NONE. Nothing else in the body may move."""
        self._write_story("candidate-story", "Candidate Story", ["deadbeef03"], ["tag:shared"])
        digest = self._digest("temperature source text")
        self._write_summary("summary-file", digest, "Warm Gadget", ["tag:shared"])

        def respond(_path, _body):
            return {"choices": [{"message": {"content": "NONE\n"}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(
                config_toml(
                    fake.url,
                    {"summarize": "cheap", "dedup": "judge"},
                    extra="[identifiers.tag]\n",
                )
            )
            code, _out = _run_quiet(self.root)

        self.assertEqual(code, 0)
        self.assertEqual(len(fake.requests), 1)
        body = fake.requests[0].body
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(sorted(body), ["messages", "model", "temperature"])

    def test_garbage_reply_gives_new_story_and_logs(self) -> None:
        self._write_story("candidate-story", "Candidate Story", ["deadbeef02"], ["tag:shared"])
        digest = self._digest("garbage reply source text")
        self._write_summary("summary-file", digest, "Odd Gadget", ["tag:shared"])

        def respond(_path, _body):
            return {"choices": [{"message": {"content": "some unrelated text\n"}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(
                config_toml(
                    fake.url,
                    {"summarize": "cheap", "dedup": "judge"},
                    extra="[identifiers.tag]\n",
                )
            )
            code, _out = _run_quiet(self.root)

        self.assertEqual(code, 0)
        self.assertTrue((self.root / "wiki" / "odd-gadget.md").is_file())
        log = (self.root / "log.md").read_text()
        self.assertIn("not a candidate or NONE", log)
        self.assertIn("some unrelated text", log)

    def test_slug_collision_gets_hash_suffix(self) -> None:
        self._write_agent("second-gadget", "Occupant", [])
        digest = self._digest("collision source text")
        self._write_summary("summary-file", digest, "Second Gadget", [])

        code, _out = _run_quiet(self.root)

        self.assertEqual(code, 0)
        suffixed = self.root / "wiki" / f"second-gadget-{digest[:8]}.md"
        self.assertTrue(suffixed.is_file())
        self.assertEqual(parse_frontmatter(suffixed.read_text())[0]["kind"], "story")
        # the pre-existing page at the un-suffixed slug is untouched
        self.assertEqual(self._fields("second-gadget")["kind"], "host")

    # -- _load_wiki non-UTF-8 (agent-kb-6jx) -----------------------------

    def test_non_utf8_page_is_skipped_not_a_crash(self) -> None:
        """`wiki/` is the user's agent's directory; the CLI cannot
        control its bytes. A page decode failure must not traceback
        `_load_wiki`, and the page stays skipped, same as any other
        unparseable page (dedup does not embed it, unlike vectors)."""
        (self.root / "wiki" / "bad.md").write_bytes(b"\xff\xfe not utf8, no frontmatter")

        from llmwiki.core import Kb

        summaries, stories, agent_pages = dedup._load_wiki(Kb(self.root))
        self.assertEqual(agent_pages, [])
        self.assertEqual(summaries, {})
        self.assertEqual(stories, {})

        code, out = _run_quiet(self.root)

        self.assertEqual(code, 0)
        self.assertIn("dedup: 0 planned", out)

    def test_lint_failing_story_is_not_kept(self) -> None:
        self._write_config(
            '[models]\nsummarize = "cheap"\n\n[identifiers.isbn]\npattern = "^[0-9]{13}$"\n'
        )
        digest = self._digest("bad isbn source text")
        self._write_summary("summary-file", digest, "Bad Widget", ["isbn:not-a-number"])
        original = (self.root / "wiki" / "summary-file.md").read_text()

        code, _out = _run_quiet(self.root)

        self.assertEqual(code, 1)
        self.assertFalse((self.root / "wiki" / "bad-widget.md").exists())
        self.assertEqual((self.root / "wiki" / "summary-file.md").read_text(), original)
        self.assertNotIn("story", self._fields("summary-file"))
        log = (self.root / "log.md").read_text()
        self.assertIn(f"{digest}: dropped (identifier-value", log)

    def test_explicit_reflag_does_not_double_place(self) -> None:
        """`dedup <H>` on an already-placed summary is a no-op: it must
        not join a second, better-scoring story and leave the digest
        listed as a member of both."""
        self._write_story("story-one", "Story One", [], ["tag:shared"])
        digest = self._digest("reflag source text")
        self._write_summary(
            "summary-file", digest, "Reflag Widget", ["tag:shared", "tag:extra"]
        )

        code, out = _run_quiet(self.root)
        self.assertEqual(code, 0)
        self.assertIn("dedup: 1 planned", out)
        self.assertEqual(self._fields("summary-file")["story"], "story-one")
        self.assertEqual(self._fields("story-one")["members"], [digest])

        # a second story now shares MORE identifiers with the summary
        # than story-one does, and would outrank it if re-scored
        self._write_story(
            "story-two", "Story Two", [], ["tag:shared", "tag:extra"]
        )

        code, out = _run_quiet(self.root, digests=[digest])

        self.assertEqual(code, 0)
        self.assertIn("dedup: 0 planned", out)
        self.assertEqual(self._fields("summary-file")["story"], "story-one")
        self.assertEqual(self._fields("story-one")["members"], [digest])
        self.assertEqual(self._fields("story-two")["members"], [])

        # each summary hash is a member of exactly one story
        member_counts: dict[str, int] = {}
        for name in ("story-one", "story-two"):
            for member in self._fields(name)["members"]:
                member_counts[member] = member_counts.get(member, 0) + 1
        self.assertEqual(member_counts.get(digest, 0), 1)

    # -- push -------------------------------------------------------

    def test_push_warns_on_agent_page_never_story(self) -> None:
        self._write_agent("agent-file", "Some Host", ["tag:shared-thing"])
        self._write_story("other-story", "Other Story", [], ["tag:shared-thing"])
        digest = self._digest("push source text")
        self._write_summary("summary-file", digest, "Push Widget", ["tag:shared-thing"])

        out_buf, err_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            code = dedup.run(self.root, None)
        out, err = out_buf.getvalue(), err_buf.getvalue()

        self.assertEqual(code, 0)
        self.assertNotIn("push\t", out)
        push_lines = [line for line in err.splitlines() if line.startswith("push\t")]
        self.assertEqual(len(push_lines), 1)
        self.assertIn("agent-file.md", push_lines[0])
        self.assertIn("tag:shared-thing", push_lines[0])
        self.assertNotIn("other-story", "\n".join(push_lines))
        log = (self.root / "log.md").read_text()
        self.assertIn("touches", log)
        self.assertIn("agent-file.md", log)

    # -- fallback -----------------------------------------------------

    def test_deterministic_fallback_picks_first_candidate_no_model_call(self) -> None:
        self._write_story("story-a", "Story A", [], ["tag:multi"])
        self._write_story("story-b", "Story B", [], ["tag:multi"])
        digest = self._digest("fallback source text")
        self._write_summary("summary-file", digest, "Fallback Widget", ["tag:multi"])

        code, _out = _run_quiet(self.root)

        self.assertEqual(code, 0)
        self.assertEqual(self._fields("story-a")["members"], [digest])
        self.assertEqual(self._fields("story-b")["members"], [])

    # -- agent-kb-yr0: a malformed [models].dedup must fail loudly,
    # never read as "dedup step not configured".

    def test_malformed_dedup_config_raises_not_silently_unset(self) -> None:
        """A typo'd or malformed [models].dedup used to be caught by
        the same `except ModelError` that unset dedup hits, so dedup
        silently took the deterministic-fallback path. It must now
        raise instead."""
        self._write_story("story-a", "Story A", [], ["tag:multi"])
        digest = self._digest("malformed dedup source text")
        self._write_summary("summary-file", digest, "Malformed Widget", ["tag:multi"])
        self._write_config('[models]\nsummarize = "cheap"\ndedup = 123\n\n[identifiers.tag]\n')

        from llmwiki.core import Kb, kb_lock
        from llmwiki.model import ModelError

        with self.assertRaises(ModelError) as ctx:
            with kb_lock(self.root):
                dedup.place(Kb(self.root), None)
        self.assertIn("[models].dedup", str(ctx.exception))
        self.assertIn("not a string", str(ctx.exception))

    # -- rebuild ----------------------------------------------------

    def test_rebuild_twice_is_stable(self) -> None:
        digest_a = self._digest("rebuild source one")
        self._write_summary(
            "summary-a", digest_a, "Widget One", ["tag:multi"], fetched="2024-01-01T00:00:00Z"
        )
        digest_b = self._digest("rebuild source two")
        self._write_summary(
            "summary-b", digest_b, "Widget Two", ["tag:multi"], fetched="2024-01-02T00:00:00Z"
        )
        code, _out = _run_quiet(self.root)
        self.assertEqual(code, 0)

        code, _out = _rebuild_quiet(self.root)
        self.assertEqual(code, 0)
        before = {path.name: path.read_text() for path in (self.root / "wiki").glob("*.md")}

        code, _out = _rebuild_quiet(self.root)
        self.assertEqual(code, 0)
        after = {path.name: path.read_text() for path in (self.root / "wiki").glob("*.md")}

        self.assertEqual(before, after)

    def test_rebuild_restores_removed_membership(self) -> None:
        digest_a = self._digest("member source one")
        self._write_summary(
            "summary-a", digest_a, "Widget One", ["tag:multi"], fetched="2024-01-01T00:00:00Z"
        )
        digest_b = self._digest("member source two")
        self._write_summary(
            "summary-b", digest_b, "Widget Two", ["tag:multi"], fetched="2024-01-02T00:00:00Z"
        )
        code, _out = _run_quiet(self.root)
        self.assertEqual(code, 0)

        story_name = self._fields("summary-a")["story"]
        self.assertEqual(story_name, self._fields("summary-b")["story"])
        self.assertEqual(self._fields(story_name)["members"], [digest_a, digest_b])

        # simulate a lost race: hand-edit the story to drop a member
        story_path = self.root / "wiki" / f"{story_name}.md"
        fields, body = parse_frontmatter(story_path.read_text())
        fields["members"] = [digest_a]
        story_path.write_text(render_frontmatter(fields, body))

        code, _out = _rebuild_quiet(self.root)
        self.assertEqual(code, 0)

        rebuilt_name = self._fields("summary-a")["story"]
        self.assertEqual(rebuilt_name, self._fields("summary-b")["story"])
        self.assertEqual(self._fields(rebuilt_name)["members"], [digest_a, digest_b])

    def test_rebuild_emits_no_push(self) -> None:
        self._write_agent("agent-file", "Some Host", ["tag:shared-thing"])
        digest = self._digest("rebuild push source text")
        self._write_summary(
            "summary-file", digest, "Push Widget", ["tag:shared-thing"], fetched="2024-01-01T00:00:00Z"
        )

        err_buf = io.StringIO()
        with redirect_stderr(err_buf):
            code, out = _run_quiet(self.root)
        self.assertEqual(code, 0)
        self.assertFalse(any(line.startswith("push\t") for line in out.splitlines()))
        self.assertTrue(
            any(line.startswith("push\t") for line in err_buf.getvalue().splitlines())
        )
        log_before = (self.root / "log.md").read_text()
        self.assertIn("## [push]", log_before)

        err_buf = io.StringIO()
        with redirect_stderr(err_buf):
            code, out = _rebuild_quiet(self.root)
        self.assertEqual(code, 0)
        self.assertFalse(any(line.startswith("push\t") for line in out.splitlines()))
        self.assertFalse(
            any(line.startswith("push\t") for line in err_buf.getvalue().splitlines())
        )

        new_log = (self.root / "log.md").read_text()[len(log_before) :]
        self.assertNotIn("## [push]", new_log)

    def test_rebuild_log_line_counts(self) -> None:
        digest_a = self._digest("log source one")
        self._write_summary(
            "summary-a", digest_a, "Widget One", ["tag:multi"], fetched="2024-01-01T00:00:00Z"
        )
        digest_b = self._digest("log source two")
        self._write_summary(
            "summary-b", digest_b, "Widget Two", ["tag:multi"], fetched="2024-01-02T00:00:00Z"
        )
        digest_c = self._digest("log source three")
        self._write_summary(
            "summary-c", digest_c, "Widget Three", ["tag:other"], fetched="2024-01-03T00:00:00Z"
        )

        code, _out = _rebuild_quiet(self.root)
        self.assertEqual(code, 0)

        log = (self.root / "log.md").read_text()
        self.assertIn("## [dedup] rebuild 3 summaries into 2 stories", log)

    def test_cli_rebuild_rejects_extra_args(self) -> None:
        code = cli.main(["--kb", str(self.root), "dedup", "--rebuild", "extra-arg"])
        self.assertEqual(code, 2)

    def test_cli_rebuild_rejects_misordered_flag(self) -> None:
        code = cli.main(["--kb", str(self.root), "dedup", "abc", "--rebuild"])
        self.assertEqual(code, 2)


class DedupPromptSubjectFramingTest(unittest.TestCase):
    """Pins agent-kb-d1w: every paragraph of DEDUP_PROMPT states story
    identity as ONE SUBJECT, and no paragraph states it as one
    real-world occurrence or one event. Measured live (same page, same
    candidate, same model): the occurrence framing returned NONE and
    rejected a correct join; the subject framing joined it, 3 of 3.

    An earlier edit swapped the first two occurrence phrases for
    subject phrases and left three occurrence-framed sentences
    standing, so the prompt stated a rule and then vetoed it. The
    pinning test of the day passed anyway, because it only checked
    that the word "subject" was present and one phrase absent. The
    vocabulary ban below is what makes that impossible to repeat: a
    surviving occurrence-identity or event-identity sentence has to
    carry one of these words."""

    OCCURRENCE_WORDS = (
        "occurrence",
        "event",
        "incident",
        "happened",
        "real-world",
    )

    def test_no_occurrence_or_event_identity_wording(self) -> None:
        lowered = DEDUP_PROMPT.lower()
        for word in self.OCCURRENCE_WORDS:
            with self.subTest(word=word):
                self.assertNotIn(word, lowered)

    def test_defines_identity_as_one_subject(self) -> None:
        self.assertIn("A story covers ONE subject.", DEDUP_PROMPT)
        self.assertIn("SAME subject", DEDUP_PROMPT)

    def test_shared_identifiers_are_still_not_evidence(self) -> None:
        self.assertIn(
            "A shared identifier is NOT by itself evidence of a match. "
            "A place, product, or person can appear in many unrelated "
            "stories.",
            DEDUP_PROMPT,
        )

    def test_preamble_claims_no_shared_identifier(self) -> None:
        """`candidates` unions identifier matches with vector
        neighbours, so a candidate can arrive with no shared identifier
        at all. On a kb with no identifier vocabulary every page carries
        `identifiers: []` and EVERY candidate arrives that way. The
        preamble must state both reasons, never assert one."""
        self.assertNotIn("already share at least one identifier", DEDUP_PROMPT)
        self.assertIn(
            "A candidate is listed because it shares an identifier with "
            "the new summary, because its text is among the nearest "
            "matches to it, or both.",
            DEDUP_PROMPT,
        )

    def test_default_to_none_instruction_tests_subject_identity(self) -> None:
        self.assertIn(
            "Default to NONE. Answer with a candidate slug only if you are "
            "confident the two pages have the SAME subject; otherwise "
            "answer NONE.",
            DEDUP_PROMPT,
        )

    def test_gut_check_tests_subject_identity(self) -> None:
        self.assertIn(
            "would a reader see one subject, or two subjects that merely "
            "share an identifier? Two means NONE.",
            DEDUP_PROMPT,
        )

    def test_one_line_reply_contract_unchanged(self) -> None:
        self.assertIn(
            'Reply with exactly one line: the candidate\'s slug, as '
            'written in its "=== CANDIDATE: <slug> ===" heading, or the '
            "literal word NONE. No other text, no explanation.",
            DEDUP_PROMPT,
        )


if __name__ == "__main__":
    unittest.main()
