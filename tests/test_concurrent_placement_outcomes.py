"""What `dedup.place` REPORTS when two writers share one kb.

Every case below already ends with the kb correct on disk. What is
under test is the number the caller is handed back, because
`ingest._pipeline` turns a short count into "this source failed" and
that is what kills the second agent's run. Avoiding `KbBusy` is not
the goal; two agents both exiting 0 is.

One process, several threads. `kb_lock` is `fcntl.flock` on a fresh
`open()` per acquire, so two threads in one process serialize on it
exactly as two OS processes do, which is why the reviewer's own
reproducer found the real interleaving with threads.
"""

import hashlib
import io
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "llm-wiki"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from llmwiki.core import (  # noqa: E402
    Kb,
    holds_kb_lock,
    kb_lock,
    parse_frontmatter,
    render_frontmatter,
)
from llmwiki import dedup, ingest, vectors  # noqa: E402
from fake_endpoint import FakeEndpoint  # noqa: E402
from kb_config import config_toml  # noqa: E402

SUMMARY_TITLE = "Report Zero"
SHARED_IDENTIFIER = "tag:shared"


def _quiet(call, *args):
    """Run `call`, swallowing the planned-count and push lines it
    prints. Returns whatever it returns."""
    with redirect_stdout(io.StringIO()):
        return call(*args)


def _fields(root: Path, stem: str) -> dict:
    return parse_frontmatter((root / "wiki" / f"{stem}.md").read_text())[0]


def _drop_lines(root: Path) -> list[str]:
    return [line for line in (root / "log.md").read_text().splitlines() if "drop" in line]


class _OneStorylessSummary(unittest.TestCase):
    """A kb holding one summary page with no `story:` field, which is
    exactly what `_target_digests` hands to `place`."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / ".kb"
        (self.root / "wiki").mkdir(parents=True)
        (self.root / "sources").mkdir()
        (self.root / "log.md").write_text("# log\n")
        (self.root / "config.toml").write_text("[identifiers.tag]\n")
        self.digest = hashlib.sha256(b"source zero").hexdigest()
        (self.root / "sources" / f"{self.digest}.txt").write_text("source zero")
        (self.root / "wiki" / "summary-zero.md").write_text(
            render_frontmatter(
                {
                    "kind": "summary",
                    "title": SUMMARY_TITLE,
                    "source": self.digest,
                    "identifiers": [SHARED_IDENTIFIER],
                    "fetched": "2026-01-01T00:00:00Z",
                },
                "Abstract for report zero.\n",
            )
        )

    def write_story(self, stem: str, title: str, members: list[str]) -> None:
        (self.root / "wiki" / f"{stem}.md").write_text(
            render_frontmatter(
                {
                    "kind": "story",
                    "title": title,
                    "members": members,
                    "identifiers": [SHARED_IDENTIFIER],
                },
                f"Body of {title}.\n",
            )
        )

    def rival_places_the_digest(self, stem: str = "rival-story") -> None:
        """What another writer's `_commit` leaves behind: a story page
        naming this digest, and the summary's `story:` back-reference."""
        self.write_story(stem, "Rival Story", [self.digest])
        path = self.root / "wiki" / "summary-zero.md"
        fields, body = parse_frontmatter(path.read_text())
        fields["story"] = stem
        path.write_text(render_frontmatter(fields, body))


class PlacedByAnotherWriter(_OneStorylessSummary):
    """dedup.py `_place_once`. A digest another writer placed while this
    one judged is WORK THAT EXISTS: the caller must be told it is
    placed, not handed a short count that `ingest` reads as a failure."""

    def test_a_digest_another_writer_placed_counts_as_placed(self) -> None:
        real_decide = dedup._decide

        def rival_commits_first(kb, summary, stories):
            placement = real_decide(kb, summary, stories)
            self.rival_places_the_digest()
            return placement

        with mock.patch.object(dedup, "_decide", rival_commits_first):
            placed, attempted = _quiet(dedup.place, Kb(self.root), None)

        self.assertEqual((placed, attempted), (1, 1))
        self.assertEqual(_fields(self.root, "summary-zero")["story"], "rival-story")
        self.assertEqual(_drop_lines(self.root), [])

    def test_run_exits_zero_when_another_writer_placed_the_digest(self) -> None:
        real_decide = dedup._decide

        def rival_commits_first(kb, summary, stories):
            placement = real_decide(kb, summary, stories)
            self.rival_places_the_digest()
            return placement

        with mock.patch.object(dedup, "_decide", rival_commits_first):
            self.assertEqual(_quiet(dedup.run, self.root, None), 0)


class TwoConcurrentPlacers(_OneStorylessSummary):
    """dedup.py `place`, ingest.py `_pipeline`. Two agents placing the
    same story-less digest at the same time must BOTH exit 0. One of
    them writes the story, the other finds the work already done, and
    neither of those is a failed run."""

    def test_both_concurrent_placers_of_one_digest_exit_zero(self) -> None:
        codes: dict[str, int] = {}
        both_decided = threading.Barrier(2, timeout=30)
        real_decide = dedup._decide

        def rendezvous_decide(kb, summary, stories):
            """Neither writer may commit before both have judged, which
            is the window `_place_once` opens by design."""
            placement = real_decide(kb, summary, stories)
            both_decided.wait()
            return placement

        def worker(name: str) -> None:
            codes[name] = dedup.run(self.root, None)

        with mock.patch.object(dedup, "_decide", rendezvous_decide):
            with redirect_stdout(io.StringIO()):
                threads = [
                    threading.Thread(target=worker, args=(name,))
                    for name in ("first", "second")
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=60)

        self.assertEqual(codes, {"first": 0, "second": 0})
        self.assertEqual(_fields(self.root, "summary-zero")["story"], "report-zero")


class CandidateVanishesMidJudge(_OneStorylessSummary):
    """dedup.py `_decide`. The judge prompt reads candidate story pages
    off disk with no lock held, so a concurrent `rebuild` can unlink one
    between the snapshot and the read. That must cost an attempt and
    re-judge, never drop the digest."""

    def test_a_candidate_vanishing_mid_judge_is_re_judged(self) -> None:
        self.write_story("acme-outage", "Acme Outage", [])
        real_decide = dedup._decide
        judged: list[int] = []

        def vanishing_decide(kb, summary, stories):
            judged.append(1)
            if len(judged) == 1:
                (self.root / "wiki" / "acme-outage.md").unlink()
                raise FileNotFoundError(str(self.root / "wiki" / "acme-outage.md"))
            return real_decide(kb, summary, stories)

        with mock.patch.object(dedup, "_decide", vanishing_decide):
            placed, attempted = _quiet(dedup.place, Kb(self.root), None)

        self.assertEqual(len(judged), 2, "the digest was never re-judged")
        self.assertEqual((placed, attempted), (1, 1))
        self.assertEqual(_drop_lines(self.root), [])


class RecreatedStoryPath(_OneStorylessSummary):
    """dedup.py `_placement_holds`. `rebuild` rebuilds story paths from
    titles, so one path can be deleted and recreated over a different
    subject. A recheck that only asks "is this path still there" reads
    that as "still holds" and appends the digest to the wrong story."""

    def test_a_path_recreated_over_a_new_subject_is_not_joined(self) -> None:
        self.write_story("acme-outage", "Acme Outage", [])
        stale = dedup._load_wiki(Kb(self.root))[1][self.root / "wiki" / "acme-outage.md"]

        def judged_against_the_old_page(kb, summary, stories):
            """The judge chose `acme-outage` while it still covered the
            Acme outage; a concurrent rebuild then recreated that path
            over an unrelated subject."""
            self.write_story("acme-outage", "Zeta Launch", [])
            return dedup.Placement(
                stale, "none", frozenset({dedup._story_subject(stale)})
            )

        with mock.patch.object(dedup, "_decide", judged_against_the_old_page):
            placed, attempted = _quiet(dedup.place, Kb(self.root), None)

        self.assertEqual((placed, attempted), (1, 1))
        self.assertEqual(
            _fields(self.root, "acme-outage")["members"],
            [],
            "the digest was appended to a story covering another subject",
        )
        self.assertNotEqual(_fields(self.root, "summary-zero")["story"], "acme-outage")


class PlacementKeepsLosingItsRace(_OneStorylessSummary):
    """dedup.py `_place_once`. A judge answering NONE while rival new
    stories keep landing used to exhaust its attempts and drop the
    digest, which `ingest` reports as a failed source that nothing ever
    retries. The digest must end up placed."""

    def test_a_placement_that_keeps_losing_its_race_is_still_placed(self) -> None:
        real_decide = dedup._decide
        rivals: list[int] = []

        def rival_story_lands_during_every_judge(kb, summary, stories):
            placement = real_decide(kb, summary, stories)
            rivals.append(1)
            self.write_story(f"rival-{len(rivals)}", f"Rival {len(rivals)}", [])
            return placement._replace(target=None)

        with mock.patch.object(dedup, "_decide", rival_story_lands_during_every_judge):
            placed, attempted = _quiet(dedup.place, Kb(self.root), None)

        self.assertEqual((placed, attempted), (1, 1))
        self.assertTrue(_fields(self.root, "summary-zero").get("story"))


class RivalStoryLandsMidJudge(_OneStorylessSummary):
    """dedup.py `_placement_holds`. Writer B judges "new story" against
    an empty wiki while writer A writes a story sharing B's identifier.
    The recheck under the lock must see that rival, re-judge, and join
    it. Committing the stale answer leaves two stories on one subject."""

    def test_a_rival_story_landing_mid_judge_is_joined_not_duplicated(self) -> None:
        rival_digest = hashlib.sha256(b"source one").hexdigest()
        (self.root / "sources" / f"{rival_digest}.txt").write_text("source one")
        (self.root / "wiki" / "summary-one.md").write_text(
            render_frontmatter(
                {
                    "kind": "summary",
                    "title": "Report One",
                    "source": rival_digest,
                    "identifiers": [SHARED_IDENTIFIER],
                    "fetched": "2026-01-02T00:00:00Z",
                },
                "Abstract for report one.\n",
            )
        )
        real_decide = dedup._decide
        judged: list[str] = []

        def writer_a_places_during_b_first_judge(kb, summary, stories):
            digest = str(summary.fields["source"])
            judged.append(digest)
            placement = real_decide(kb, summary, stories)
            if digest == self.digest and judged.count(self.digest) == 1:
                writer_a = threading.Thread(
                    target=_quiet, args=(dedup.place, Kb(self.root), [rival_digest])
                )
                writer_a.start()
                writer_a.join(timeout=60)
            return placement

        with mock.patch.object(dedup, "_decide", writer_a_places_during_b_first_judge):
            placed, attempted = _quiet(dedup.place, Kb(self.root), [self.digest])

        self.assertEqual((placed, attempted), (1, 1))
        self.assertEqual(judged.count(self.digest), 2, "writer B was never re-judged")
        rival_story = _fields(self.root, "summary-one")["story"]
        self.assertEqual(_fields(self.root, "summary-zero")["story"], rival_story)
        stories = [
            path.stem
            for path in (self.root / "wiki").glob("*.md")
            if _fields(self.root, path.stem)["kind"] == "story"
        ]
        self.assertEqual(stories, [rival_story])


class PlaceRefusesUnderTheKbLock(_OneStorylessSummary):
    """dedup.py `place`. Its "CALLER MUST NOT HOLD kb_lock" contract was
    enforced by a docstring alone. `kb_lock` is not re-entrant, so
    breaking it stalls for the whole lock timeout and then dies with an
    unrelated `KbBusy`. It must name the real mistake at once."""

    def test_place_refuses_at_once_when_the_caller_holds_the_kb_lock(self) -> None:
        started = time.monotonic()
        with kb_lock(self.root):
            with self.assertRaises(RuntimeError) as caught:
                _quiet(dedup.place, Kb(self.root), None)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertIn("kb lock", str(caught.exception))


class EmbedCallsOutsideTheKbLock(_OneStorylessSummary):
    """ingest.py `_pipeline`. The sweep after placement used to run
    under the kb lock, which put an embed call AND a walk and hash of
    the whole wiki inside it. That hold is D9 all over again: long,
    paid, and shared, so the next agent waits it out. Every embed call
    on the real ingest path must happen with no lock held."""

    def _respond(self, path: str, body: dict) -> dict:
        if path == "/embeddings":
            return {
                "data": [
                    {"index": index, "embedding": [float(len(text)), 1.0, 0.0]}
                    for index, text in enumerate(body["input"])
                ]
            }
        content = body["messages"][0]["content"]
        if "=== NEW SUMMARY ===" in content:
            return {"choices": [{"message": {"content": "NONE"}}]}
        page = (
            "---\nkind: summary\ntitle: Report One\n"
            f"identifiers: [{SHARED_IDENTIFIER}]\n---\n\nAbstract for report one.\n"
        )
        return {"choices": [{"message": {"content": page}}]}

    def test_no_embed_call_runs_while_the_kb_lock_is_held(self) -> None:
        source = self.tmp / "report-one.md"
        source.write_text("source one text")
        held_at_embed: list[bool] = []
        real_embed = vectors.embed

        def recording_embed(target, texts):
            held_at_embed.append(holds_kb_lock())
            return real_embed(target, texts)

        with FakeEndpoint(self._respond) as fake:
            (self.root / "config.toml").write_text(
                config_toml(
                    fake.url,
                    {"summarize": "cheap", "dedup": "cheap", "embed": "cheap"},
                    extra=f"[identifiers.{SHARED_IDENTIFIER.split(':')[0]}]\n",
                )
            )
            with mock.patch.object(vectors, "embed", recording_embed):
                self.assertEqual(_quiet(ingest.run, self.root, [str(source)]), 0)

        self.assertTrue(held_at_embed, "no embed call was made, so nothing was proved")
        self.assertNotIn(True, held_at_embed)


if __name__ == "__main__":
    unittest.main()
