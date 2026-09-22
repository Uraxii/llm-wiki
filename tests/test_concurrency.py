"""Reproduces concurrency hazards from the audit: N separate `llmwiki`
OS processes sharing one `.kb` with no locking anywhere in the package
(grep for flock/fcntl/lockf/LOCK_EX/O_EXCL over llmwiki/*.py: zero hits).

Rerunnable, one process per run:

    .venv/bin/python -m unittest tests.test_concurrency -v

Each test spawns real `sys.executable -m llmwiki` OS subprocesses
against one shared tempdir kb, so a coming `fcntl.flock` fix actually
applies (a lock only guards a single process's own file descriptor
table; two separate `open()` calls, one per process, are what a
process-level lock exists to serialize). Each test asserts the
CORRECT, race-free invariant, the same thing that fix must make true.
Against today's unmodified llmwiki the assertion is false, so the test
fails (red): that failure IS the proof the hazard is real. Once the
fix lands, the same assertion goes true and the same test goes green,
no test edit needed.

Hazard 1 (STORY LOST UPDATE, dedup.py:465 `dedup.run`, :295
`_load_wiki`, :149 `judge`, :247/:253 the member-list and
back-reference writes). The coming fix is a lock spanning the WHOLE
`dedup.run`, so this test never makes both processes rendezvous inside
that span (that would deadlock once the span is serialized, see the
test's own docstring). Instead process B is held open on its one
network call by an `Event` only the main thread sets, and the main
thread gives process A a bounded window to finish before setting it,
so B always acts on a snapshot no fresher than the moment A started,
exactly the interleaving the audit describes.

Hazard 3 (SUMMARY SLUG COLLISION, summarize.py:169 `candidate.exists`,
:221 `atomic_write_text`). The coming fix is a narrow atomic slug claim
around the check-then-write alone, not the model call before it, so
both processes CAN still rendezvous at that model call after the fix
lands. `FakeEndpoint(concurrent=True)` holds the first summarize
request open until a second, real, concurrent request from the other
process arrives, then releases both, so both processes reach the slug
check at the same instant, every run, not by luck.
"""

import contextlib
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "llm-wiki"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from llmwiki.core import Kb, as_list, parse_frontmatter, render_frontmatter, slugify  # noqa: E402
from llmwiki.core import kb_lock as real_kb_lock  # noqa: E402
from llmwiki.model import chat as real_chat  # noqa: E402
from llmwiki import dedup, ingest, summarize, vectors  # noqa: E402
from fake_endpoint import FakeEndpoint  # noqa: E402
from kb_config import config_toml  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def _subprocess_env() -> dict:
    return {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "skills" / "llm-wiki")])}


def _fields(root: Path, name: str) -> dict:
    text = (root / "wiki" / f"{name}.md").read_text(encoding="utf-8")
    return parse_frontmatter(text)[0]


class Hazard1LostUpdate(unittest.TestCase):
    """dedup.py:465. Two `llmwiki dedup <digest>` OS processes join one
    shared story; process A's write must not be overwritten by process
    B acting on a snapshot taken before A wrote."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / ".kb"
        for sub in ("wiki", "sources"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "log.md").write_text("# log\n")
        (self.root / "wiki" / "foo.md").write_text(
            render_frontmatter(
                {"kind": "story", "title": "Foo", "members": [],
                 "identifiers": ["tag:shared"]},
                "placeholder body",
            )
        )
        self.digest1 = self._summary("s1", "Summary One")
        self.digest2 = self._summary("s2", "Summary Two")

    def _summary(self, name: str, title: str) -> str:
        digest = hashlib.sha256(name.encode()).hexdigest()
        (self.root / "sources" / f"{digest}.md").write_text(f"{name} source")
        (self.root / "wiki" / f"{name}.md").write_text(
            render_frontmatter(
                {"kind": "summary", "title": title, "source": digest,
                 "identifiers": ["tag:shared"]},
                f"Abstract for {title}.",
            )
        )
        return digest

    def _dedup(self, digest: str, env: dict) -> subprocess.Popen:
        cmd = [sys.executable, "-m", "llmwiki", "--kb", str(self.root), "dedup", digest]
        return subprocess.Popen(
            cmd, cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_no_lost_update_no_dangling_story_reference(self) -> None:
        """The correct, race-free outcome: both digests end up members
        of foo.md, and neither summary's `story:` field points at a
        story that does not list it back. Fails today: B's write
        overwrites foo.md from its stale members=[], dropping digest1
        while s1.md still claims `story: foo`, a dangling
        back-reference no lint check covers.

        How the fix makes it true: `_place_once` judges with no lock
        held, then takes the lock and re-reads the wiki, and `_commit`
        rebuilds `members` from THAT read rather than from the story
        object the judge was shown. B's stale `members=[]` is therefore
        never written, whichever process commits second.

        Deadlock check: only process B ever waits, on an `Event` only
        the main thread sets, and B's judge call is outside the lock, so
        `b_reached` fires with nothing held. Process A is never made to
        wait on anything from this test; it takes the same lock only for
        its own short commit. Each process's `communicate` is bounded,
        and nothing here waits on A before releasing B.
        """
        b_reached = threading.Event()
        release_b = threading.Event()

        def respond(_path: str, body: dict) -> dict:
            content = body["messages"][0]["content"]
            if self.digest2 in content:
                b_reached.set()
                release_b.wait(timeout=20)
            return {"choices": [{"message": {"content": "foo"}}]}

        env = _subprocess_env()
        with FakeEndpoint(respond, concurrent=True) as fake:
            (self.root / "config.toml").write_text(
                config_toml(
                    fake.url,
                    {"summarize": "cheap", "dedup": "cheap"},
                    extra="[identifiers.tag]\n",
                )
            )
            proc_b = self._dedup(self.digest2, env)
            self.assertTrue(
                b_reached.wait(timeout=15),
                "process B never reached its judge call",
            )

            proc_a = self._dedup(self.digest1, env)
            try:
                out_a, err_a = proc_a.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                # Only expected once the coming lock serializes A
                # behind B; release B and let A's blocked acquire
                # resolve on its own.
                out_a = err_a = None

            release_b.set()
            out_b, err_b = proc_b.communicate(timeout=20)
            self.assertEqual(proc_b.returncode, 0, err_b)

            if out_a is None:
                out_a, err_a = proc_a.communicate(timeout=20)
            self.assertEqual(proc_a.returncode, 0, err_a)

        members = _fields(self.root, "foo")["members"]
        s1_fields = _fields(self.root, "s1")
        s2_fields = _fields(self.root, "s2")

        self.assertIn(
            self.digest1, members,
            "lost update: digest1's membership was overwritten away "
            f"by the second writer, members={members!r}",
        )
        self.assertIn(self.digest2, members)
        self.assertEqual(s1_fields["story"], "foo")
        self.assertEqual(s2_fields["story"], "foo")


class Hazard2DuplicateStory(unittest.TestCase):
    """dedup.py:502 `dedup.run`, :102 `candidates`. Two `llmwiki dedup
    <digest>` OS processes for summaries that share an identifier, with
    no story for that occurrence on disk yet, must not each start their
    own: one occurrence gets one story, not two."""

    PADDING_DIGESTS = 250  # see test docstring: widens the race window

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / ".kb"
        for sub in ("wiki", "sources"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "log.md").write_text("# log\n")
        # No [models] section at all: with no dedup model configured
        # (the project's own recommended default, dedup.py:499-500),
        # `judge` never calls the endpoint, so this hazard needs no
        # FakeEndpoint and no stall to reach the race.
        (self.root / "config.toml").write_text(
            "[identifiers.tag]\n[identifiers.padding]\n"
        )
        self.digest1 = self._summary("s1", "Alpha Event", "tag:shared")
        self.digest2 = self._summary("s2", "Beta Event", "tag:shared")
        # One process's own targets, ahead of digest1: unrelated
        # digests that all share ONE identifier with each other (never
        # with tag:shared), so they fold into one padding story instead
        # of growing the wiki by one page each. Each still runs its own
        # `_load_wiki`-independent judge/write/self-lint pass, which is
        # what spends the wall-clock time this test needs; see the test
        # docstring for why the time itself, not the page count, is the
        # point.
        self.padding = [
            self._summary(f"pad{i}", "Padding", "padding:x")
            for i in range(self.PADDING_DIGESTS)
        ]

    def _summary(self, name: str, title: str, identifier: str) -> str:
        digest = hashlib.sha256(name.encode()).hexdigest()
        (self.root / "sources" / f"{digest}.md").write_text(f"{name} source")
        (self.root / "wiki" / f"{name}.md").write_text(
            render_frontmatter(
                {"kind": "summary", "title": title, "source": digest,
                 "identifiers": [identifier]},
                f"Abstract for {title}.",
            )
        )
        return digest

    def _dedup(self, digests: list[str], env: dict) -> subprocess.Popen:
        cmd = [sys.executable, "-m", "llmwiki", "--kb", str(self.root), "dedup", *digests]
        return subprocess.Popen(
            cmd, cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_one_story_not_two_for_one_occurrence(self) -> None:
        """The correct, race-free outcome: one story page holds both
        digests as members. Fails today: each process's own `_load_wiki`
        snapshot, taken once at process start before either has written
        anything, sees no story yet; each independently starts one; two
        story pages end up sharing `tag:shared`, one occurrence split in
        two, with no error and no dropped count on either side (both
        processes exit 0, and the placed count matches the requested
        count on each side alone), so no other gate here would catch it.

        No model call is available to stall this hazard on (empty
        candidates, dedup.py:157-158, return before `judge` ever touches
        the network), so the race is pinned by wall-clock work instead:
        process A is handed `digest1` behind `PADDING_DIGESTS` unrelated
        digests, each a real judge/write/self-lint pass over the
        now-real wiki on disk. `_load_wiki` runs ONCE, before that loop,
        so this padding delays only when A reaches `digest1`'s write,
        never what A's stale snapshot already decided; process B, given
        only `digest2`, easily finishes first. Measured at
        PADDING_DIGESTS=250: 10/10 on this pre-fix tree. Runtime cost is
        real (padding is real work, not a sleep) and paid by this test
        alone.

        Deadlock check: neither process is made to wait on the other by
        this test; only the coming `kb_lock` (bounded at
        LOCK_WAIT_TIMEOUT_SEC, past which it raises KbBusy rather than
        blocking forever) can make one wait on the other, so a 60s
        `communicate` timeout, comfortably past that 30s bound, cannot
        mistake a bounded wait for a hang.
        """
        env = _subprocess_env()
        proc_a = self._dedup([*self.padding, self.digest1], env)
        proc_b = self._dedup([self.digest2], env)
        out_a, err_a = proc_a.communicate(timeout=60)
        out_b, err_b = proc_b.communicate(timeout=60)
        self.assertEqual(proc_a.returncode, 0, err_a)
        self.assertEqual(proc_b.returncode, 0, err_b)

        story_paths = []
        for path in sorted((self.root / "wiki").glob("*.md")):
            fields = parse_frontmatter(path.read_text(encoding="utf-8"))[0]
            if fields.get("kind") == "story" and "tag:shared" in as_list(
                fields.get("identifiers")
            ):
                story_paths.append(path)
        self.assertEqual(
            len(story_paths), 1,
            f"two stories for one occurrence: {[p.name for p in story_paths]!r}",
        )

        members = as_list(
            parse_frontmatter(story_paths[0].read_text(encoding="utf-8"))[0].get(
                "members"
            )
        )
        self.assertIn(self.digest1, members)
        self.assertIn(self.digest2, members)
        self.assertEqual(_fields(self.root, "s1")["story"], story_paths[0].stem)
        self.assertEqual(_fields(self.root, "s2")["story"], story_paths[0].stem)


class Hazard3SlugCollision(unittest.TestCase):
    """summarize.py:169/:221. Two `llmwiki summarize <digest>` OS
    processes whose model-chosen titles slugify identically must not
    let one process's write destroy the other's summary page."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / ".kb"
        for sub in ("wiki", "sources"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "log.md").write_text("# log\n")

    def _store_source(self, text: str) -> str:
        digest = hashlib.sha256(text.encode()).hexdigest()
        (self.root / "sources" / f"{digest}.md").write_text(text)
        # The provenance sidecar `store()` (sources.py) would have
        # already written on ingest: "already exists" per the audit.
        (self.root / "sources" / f"{digest}.toml").write_text(
            'url = "https://example.com/x"\n'
            'fetched = "2024-01-01T00:00:00Z"\n'
            'content_type = "text/markdown"\n'
            'job = "manual"\n'
        )
        return digest

    def _summarize(self, digest: str, env: dict) -> subprocess.Popen:
        cmd = [sys.executable, "-m", "llmwiki", "--kb", str(self.root), "summarize", digest]
        return subprocess.Popen(
            cmd, cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_both_summaries_survive_the_collision(self) -> None:
        """The correct, race-free outcome: both digests keep a summary
        page that claims them as `source`. Fails today: the second
        write clobbers the first digest's page at the shared slug, and
        the destroyed digest's source sidecar already exists (written
        above, as `sources.store` would have on ingest) so nothing
        about the source itself signals retry; `status` reports it
        exactly as if it had never been summarized.

        Deadlock check: the coming fix is a narrow atomic claim around
        the slug check-then-write alone (summarize.py:169/:221), not
        around the model call ahead of it, so both processes can still
        rendezvous here after the fix lands, same as before it. Once
        both are released they either race a non-blocking create (one
        wins the plain slug, the other falls back to its
        digest-suffixed one) or contend a lock scoped to that same
        narrow span, which one of them always then holds only long
        enough to finish its own quick write and release. Nothing in
        this test waits on anything past that release, so it cannot
        hang on either implementation.
        """
        digest1 = self._store_source("first source text")
        digest2 = self._store_source("second source text")

        arrived = 0
        arrived_lock = threading.Lock()
        both_arrived = threading.Event()

        def respond(_path: str, _body: dict) -> dict:
            nonlocal arrived
            with arrived_lock:
                arrived += 1
                if arrived == 2:
                    both_arrived.set()
            # Same title for every request: both digests slugify to
            # "collision-title", the collision the audit describes.
            both_arrived.wait(timeout=20)
            return {"choices": [{"message": {"content": (
                "---\nkind: summary\ntitle: Collision Title\n"
                "identifiers: []\n---\n\nAn abstract.\n"
            )}}]}

        env = _subprocess_env()
        with FakeEndpoint(respond, concurrent=True) as fake:
            (self.root / "config.toml").write_text(
                config_toml(fake.url, {"summarize": "cheap"}, extra="[identifiers.tag]\n")
            )
            proc_a = self._summarize(digest1, env)
            proc_b = self._summarize(digest2, env)
            out_a, err_a = proc_a.communicate(timeout=30)
            out_b, err_b = proc_b.communicate(timeout=30)

        self.assertEqual(proc_a.returncode, 0, err_a)
        self.assertEqual(proc_b.returncode, 0, err_b)

        kb = Kb(self.root)
        index_after = summarize._summary_index(kb)
        missing = {digest1, digest2} - set(index_after)
        self.assertEqual(
            missing, set(),
            f"summary destroyed by the second writer: {index_after!r}",
        )

        with redirect_stdout(io.StringIO()) as buf:
            vectors.status(self.root)
        for digest in (digest1, digest2):
            self.assertNotIn(f"{digest}\tno summary", buf.getvalue())


class Hazard4RollbackDeletesRival(unittest.TestCase):
    """dedup.py:249 `_write_story` and its rollback. A brand-new story
    one process's own self-lint drops must not take down the SAME-slug
    story page a second, independent process meanwhile wrote for real:
    `story_prev` is only `None` (the "nothing to unlink" belief) because
    THIS process's own snapshot never saw a rival write, not because
    nothing is there when the rollback actually runs."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / ".kb"
        for sub in ("wiki", "sources"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "log.md").write_text("# log\n")
        # A pre-existing decoy story sharing the identifier, so both
        # processes' own `candidates()` calls are non-empty and both
        # DO reach `judge`'s model call (dedup.py:157-158 skips it
        # outright for an empty list): the stall this test needs to
        # pin the interleave.
        (self.root / "wiki" / "decoy.md").write_text(
            render_frontmatter(
                {"kind": "story", "title": "Decoy", "members": [],
                 "identifiers": ["tag:shared"]},
                "placeholder body",
            )
        )
        # Same title on both summaries: `_story_path`
        # (pre-fix)/`_free_story_path` (post-fix) computes the
        # SAME candidate slug for both once each rejects the decoy,
        # which is what gives the pre-fix TOCTOU something to collide
        # on. `digest_a`'s summary alone carries `bogus:oops`, an
        # identifier key `config.toml` never declares: the cheapest of
        # the six lint checks to trip on demand (`identifier-key`,
        # lint.py:50), and it lives on `sa.md` itself, so it fails
        # every time regardless of which process's content is
        # currently sitting at the shared story path.
        self.digest_a = self._summary("sa", "Collision Title", "bogus:oops")
        self.digest_b = self._summary("sb", "Collision Title")

    def _summary(self, name: str, title: str, *extra_identifiers: str) -> str:
        digest = hashlib.sha256(name.encode()).hexdigest()
        (self.root / "sources" / f"{digest}.md").write_text(f"{name} source")
        (self.root / "wiki" / f"{name}.md").write_text(
            render_frontmatter(
                {"kind": "summary", "title": title, "source": digest,
                 "identifiers": ["tag:shared", *extra_identifiers]},
                f"Abstract for {title}.",
            )
        )
        return digest

    def _dedup(self, digest: str, env: dict) -> subprocess.Popen:
        cmd = [sys.executable, "-m", "llmwiki", "--kb", str(self.root), "dedup", digest]
        return subprocess.Popen(
            cmd, cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_no_dangling_story_reference(self) -> None:
        """The correct, race-free outcome: `sb.md`'s `story:` field,
        once dedup places it, names a page that actually exists.
        `sa.md`'s own placement is EXPECTED to be dropped every run
        (its own self-lint always trips on `bogus:oops`) and its
        process is expected to exit 1 for that reason alone; this test
        never asserts otherwise. Fails today: `sa`'s rollback
        (`story.path.unlink()`, taken because `sa`'s OWN in-process
        belief was "I just created this slug") removes
        `collision-title.md` out from under `sb`, which had already
        written its real, clean content there and exited 0 believing
        it succeeded. Measured at 20/20 over 20 consecutive pre-fix
        runs; see this test's report for the count this run.

        Both requests to the FakeEndpoint wait on the SAME `Event`,
        set once two arrivals are counted OR after a 2s cap, whichever
        first: pre-fix, with no lock at all, both requests arrive
        quickly and get released together, which is what gives the
        TOCTOU in `_story_path` (pre-fix) something to race on. The
        judge call still runs outside the lock after the fix, so both
        arrivals are still counted and both are still released
        together; what changed is that `_free_story_path` and the write
        now run inside a short lock, and the loser's recheck sees the
        winner's new story as a candidate it was never shown, re-judges
        once, and lands on its own digest-suffixed path. Its rollback
        can then only unlink a page it really did create.

        That 2s cap, well under this test's own 60s `communicate`
        timeout, is why this cannot hang. The re-judge is bounded at
        PLACEMENT_ATTEMPTS, so the endpoint is called a fixed number of
        times, never in a loop.
        """
        arrived = 0
        arrived_lock = threading.Lock()
        released = threading.Event()

        def respond(_path: str, _body: dict) -> dict:
            nonlocal arrived
            with arrived_lock:
                arrived += 1
                if arrived >= 2:
                    released.set()
            released.wait(timeout=2)
            return {"choices": [{"message": {"content": "NONE"}}]}

        env = _subprocess_env()
        with FakeEndpoint(respond, concurrent=True) as fake:
            (self.root / "config.toml").write_text(
                config_toml(fake.url, {"dedup": "cheap"}, extra="[identifiers.tag]\n")
            )
            proc_a = self._dedup(self.digest_a, env)
            proc_b = self._dedup(self.digest_b, env)
            out_a, err_a = proc_a.communicate(timeout=60)
            out_b, err_b = proc_b.communicate(timeout=60)

        # sa's own placement is always dropped by design; only sb's
        # outcome is asserted.
        self.assertEqual(proc_b.returncode, 0, err_b)

        sb_story = _fields(self.root, "sb").get("story")
        self.assertTrue(sb_story, f"sb never got placed: {err_b}")
        self.assertTrue(
            (self.root / "wiki" / f"{sb_story}.md").exists(),
            f"sb.md points at {sb_story!r}, which does not exist: "
            "another process's rollback deleted it out from under a "
            "writer that had already committed and exited 0",
        )


class D9ConcurrentIngestWithJudge(unittest.TestCase):
    """ingest.py:91 and dedup.py:551. Two `llmwiki ingest` OS processes
    against one kb with `[models] dedup` set must both exit 0. Today the
    first process holds the kb lock across its judge call, so the second
    waits out LOCK_WAIT_TIMEOUT_SEC and dies with KbBusy."""

    ALPHA_SOURCE = "alpha source text"
    BETA_SOURCE = "beta source text"
    ALPHA_TITLE = "Alpha Report"
    BETA_TITLE = "Beta Report"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / ".kb"
        for sub in ("wiki", "sources"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "log.md").write_text("# log\n")
        # A story both summaries can join, so `candidates` is non-empty
        # and `judge` actually reaches the endpoint (dedup.py:208-209
        # returns before the call for an empty candidate list).
        (self.root / "wiki" / "foo.md").write_text(
            render_frontmatter(
                {"kind": "story", "title": "Foo", "members": [],
                 "identifiers": ["tag:shared"]},
                "placeholder body",
            )
        )
        self.alpha_path = self._source_file("alpha.md", self.ALPHA_SOURCE)
        self.beta_path = self._source_file("beta.md", self.BETA_SOURCE)

    def _source_file(self, name: str, text: str) -> Path:
        # Outside the kb: `ingest` reads the file and stores the bytes
        # itself, which is the path D9 lives on.
        path = self.tmp / name
        path.write_text(text)
        return path

    def _summary_reply(self, title: str) -> dict:
        page = (
            f"---\nkind: summary\ntitle: {title}\n"
            "identifiers: [tag:shared]\n---\n\n"
            f"Abstract for {title}.\n"
        )
        return {"choices": [{"message": {"content": page}}]}

    def _ingest(self, source: Path, env: dict) -> subprocess.Popen:
        cmd = [sys.executable, "-m", "llmwiki", "--kb", str(self.root),
               "ingest", str(source)]
        return subprocess.Popen(
            cmd, cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_concurrent_ingest_with_a_judge_model(self) -> None:
        """The correct outcome: both ingests exit 0 and both summaries
        join `foo`. Fails today: process beta reaches its judge call
        holding the kb lock for the whole of `dedup.place`, so process
        alpha's own summarize commit lock waits out
        LOCK_WAIT_TIMEOUT_SEC and raises KbBusy, which `_ingest_one`
        turns into `summarize failed` and a nonzero exit.

        Deadlock check: only the beta process is ever made to wait, on
        an `Event` this thread sets once alpha has finished or timed
        out. Alpha waits on nothing this test controls. Beta's stall
        therefore lasts as long as alpha takes, which is exactly the
        pre-fix 30s lock wait, and is bounded by the endpoint's own 60s
        timeout (model.py:16) either way.
        """
        beta_judging = threading.Event()
        release_beta = threading.Event()

        def respond(_path: str, body: dict) -> dict:
            content = body["messages"][0]["content"]
            if "=== NEW SUMMARY ===" not in content:
                title = (
                    self.BETA_TITLE if self.BETA_SOURCE in content
                    else self.ALPHA_TITLE
                )
                return self._summary_reply(title)
            if f"title: {self.BETA_TITLE}" in content:
                beta_judging.set()
                release_beta.wait(timeout=90)
            return {"choices": [{"message": {"content": "foo"}}]}

        env = _subprocess_env()
        with FakeEndpoint(respond, concurrent=True) as fake:
            (self.root / "config.toml").write_text(
                config_toml(
                    fake.url,
                    {"summarize": "cheap", "dedup": "cheap"},
                    extra="[identifiers.tag]\n",
                )
            )
            proc_beta = self._ingest(self.beta_path, env)
            self.assertTrue(
                beta_judging.wait(timeout=30),
                "the beta process never reached its judge call",
            )

            proc_alpha = self._ingest(self.alpha_path, env)
            try:
                out_alpha, err_alpha = proc_alpha.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                out_alpha = err_alpha = None

            release_beta.set()
            out_beta, err_beta = proc_beta.communicate(timeout=60)
            if out_alpha is None:
                out_alpha, err_alpha = proc_alpha.communicate(timeout=60)

        self.assertEqual(
            proc_alpha.returncode, 0,
            "the second agent's ingest failed while the first held the "
            f"kb lock across its judge call: {err_alpha}",
        )
        self.assertEqual(proc_beta.returncode, 0, err_beta)

        alpha_fields = _fields(self.root, slugify(self.ALPHA_TITLE))
        beta_fields = _fields(self.root, slugify(self.BETA_TITLE))
        self.assertEqual(alpha_fields["story"], "foo")
        self.assertEqual(beta_fields["story"], "foo")
        members = _fields(self.root, "foo")["members"]
        self.assertIn(alpha_fields["source"], members)
        self.assertIn(beta_fields["source"], members)


class ModelCallsOutsideTheKbLock(unittest.TestCase):
    """ingest.py `_pipeline`. A model call made while this process holds
    the kb lock IS D9: it is what makes the hold outlast every other
    agent's bounded wait. This watches the real ingest path and records
    the lock depth at each call instead of reading the code and
    believing it.

    This kb configures no embed model, so the embed call the trailing
    `vectors.sweep` would make is not exercised here. It holds no lock
    either; `EmbedCallsOutsideTheKbLock` in
    tests/test_concurrent_placement_outcomes.py is what proves that."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / ".kb"
        for sub in ("wiki", "sources"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "log.md").write_text("# log\n")
        (self.root / "wiki" / "foo.md").write_text(
            render_frontmatter(
                {"kind": "story", "title": "Foo", "members": [],
                 "identifiers": ["tag:shared"]},
                "placeholder body",
            )
        )
        self.source = self.tmp / "one.md"
        self.source.write_text("one source text")

    def test_no_model_call_runs_while_the_kb_lock_is_held(self) -> None:
        """Drives `ingest.run` end to end and asserts every `chat` call,
        the summarizer's and the judge's alike, happened at lock depth
        zero. Before the fix the judge ran at depth 1."""
        depth = 0
        calls: list[tuple[str, int]] = []

        @contextlib.contextmanager
        def counting_lock(root: Path):
            nonlocal depth
            with real_kb_lock(root):
                depth += 1
                try:
                    yield
                finally:
                    depth -= 1

        def counting_chat(step: str):
            def wrapper(*args, **kwargs):
                calls.append((step, depth))
                return real_chat(*args, **kwargs)

            return wrapper

        def respond(_path: str, body: dict) -> dict:
            content = body["messages"][0]["content"]
            if "=== NEW SUMMARY ===" in content:
                return {"choices": [{"message": {"content": "foo"}}]}
            page = (
                "---\nkind: summary\ntitle: One Report\n"
                "identifiers: [tag:shared]\n---\n\nAbstract for One Report.\n"
            )
            return {"choices": [{"message": {"content": page}}]}

        with FakeEndpoint(respond) as fake:
            (self.root / "config.toml").write_text(
                config_toml(
                    fake.url,
                    {"summarize": "cheap", "dedup": "cheap"},
                    extra="[identifiers.tag]\n",
                )
            )
            patches = [
                unittest.mock.patch.object(module, "kb_lock", counting_lock)
                for module in (dedup, summarize)
            ] + [
                unittest.mock.patch.object(module, "chat", counting_chat(step))
                for module, step in ((dedup, "dedup"), (summarize, "summarize"))
            ]
            with contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                with redirect_stdout(io.StringIO()):
                    code = ingest.run(self.root, [str(self.source)])

        self.assertEqual(code, 0)
        self.assertEqual(
            sorted(name for name, _depth in calls), ["dedup", "summarize"],
            f"the ingest path did not make both model calls: {calls!r}",
        )
        self.assertEqual(
            [held for _name, held in calls], [0, 0],
            f"a model call ran while the kb lock was held: {calls!r}",
        )
        self.assertEqual(_fields(self.root, "one-report")["story"], "foo")


if __name__ == "__main__":
    unittest.main()
