"""Storing source bytes by hash, with a TOML provenance sidecar."""

import hashlib
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "llm-wiki"))
from llmwiki.core import Kb  # noqa: E402
from llmwiki.sources import read_provenance, store, stored_urls  # noqa: E402


class SourcesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        root = tmp / ".kb"
        root.mkdir()
        self.kb = Kb(root)
        self.kb.sources.mkdir(parents=True)

    def test_store_twice_is_idempotent(self) -> None:
        data = b"same bytes every time"
        digest, status = store(
            self.kb, data, "https://x/1", "text/markdown", "job-1"
        )
        self.assertEqual(status, "new")
        byte_path = self.kb.sources / f"{digest}.md"
        sidecar_path = self.kb.sources / f"{digest}.toml"
        content_before = byte_path.read_bytes()
        mtime_before = byte_path.stat().st_mtime_ns
        provenance_before = sidecar_path.read_text(encoding="utf-8")

        digest2, status2 = store(
            self.kb, data, "https://x/2", "text/markdown", "job-2"
        )

        self.assertEqual(digest2, digest)
        self.assertEqual(status2, "exists")
        self.assertEqual(byte_path.read_bytes(), content_before)
        self.assertEqual(byte_path.stat().st_mtime_ns, mtime_before)
        self.assertEqual(
            sidecar_path.read_text(encoding="utf-8"), provenance_before
        )

    def test_concurrent_store_of_same_bytes(self) -> None:
        data = b"raced by two writers"
        errors = []
        results = []

        def worker() -> None:
            try:
                results.append(
                    store(self.kb, data, "https://race", "text/plain", "job")
                )
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        digest = results[0][0]
        self.assertTrue(all(result[0] == digest for result in results))
        self.assertEqual({result[1] for result in results}, {"new", "exists"})
        byte_files = [
            path
            for path in self.kb.sources.glob(f"{digest}.*")
            if path.suffix != ".toml"
        ]
        self.assertEqual(len(byte_files), 1)
        self.assertEqual(byte_files[0].read_bytes(), data)

    def test_extension_by_content_type(self) -> None:
        cases = [
            ("text/markdown", ".md"),
            ("application/pdf", ".pdf"),
            ("text/html", ".txt"),
            ("application/x-unknown", ".txt"),
            ("text/markdown; charset=utf-8", ".md"),
        ]
        for index, (content_type, expected_ext) in enumerate(cases):
            data = f"payload {index}".encode()
            digest, status = store(self.kb, data, "https://x", content_type, "job")
            self.assertEqual(status, "new")
            self.assertTrue((self.kb.sources / f"{digest}{expected_ext}").is_file())

    def test_provenance_round_trip_escaped_url(self) -> None:
        url = 'https://example.test/path?q="quoted"\\backslash'
        digest, _status = store(
            self.kb, b"escaped url payload", url, "text/plain", "job"
        )
        provenance = read_provenance(self.kb, digest)
        self.assertEqual(provenance["url"], url)
        self.assertEqual(provenance["content_type"], "text/plain")
        self.assertEqual(provenance["job"], "job")
        self.assertIn("fetched", provenance)

    def test_provenance_round_trip_non_ascii_url(self) -> None:
        url = "https://example.test/wiki/日本語"
        digest, _status = store(
            self.kb, b"non-ascii url payload", url, "text/plain", "job"
        )
        provenance = read_provenance(self.kb, digest)
        self.assertEqual(provenance["url"], url)

    def test_same_bytes_different_content_type_is_exists(self) -> None:
        data = b"same bytes, different content type"
        digest, status = store(
            self.kb, data, "https://x/first", "text/markdown", "job-first"
        )
        self.assertEqual(status, "new")
        byte_path = self.kb.sources / f"{digest}.md"
        sidecar_path = self.kb.sources / f"{digest}.toml"
        provenance_before = sidecar_path.read_text(encoding="utf-8")

        digest2, status2 = store(
            self.kb, data, "https://x/second", "application/pdf", "job-second"
        )

        self.assertEqual(digest2, digest)
        self.assertEqual(status2, "exists")
        names = sorted(path.name for path in self.kb.sources.iterdir())
        self.assertEqual(names, sorted([f"{digest}.md", f"{digest}.toml"]))
        self.assertFalse((self.kb.sources / f"{digest}.pdf").exists())
        self.assertTrue(byte_path.is_file())
        self.assertEqual(
            sidecar_path.read_text(encoding="utf-8"), provenance_before
        )
        provenance = read_provenance(self.kb, digest)
        self.assertEqual(provenance["job"], "job-first")

    def test_concurrent_store_of_same_bytes_different_content_type(self) -> None:
        data = b"raced by two writers, two content types"
        errors = []
        results = []
        content_types = ["text/markdown", "application/pdf"]

        def worker(content_type: str) -> None:
            try:
                results.append(
                    store(self.kb, data, "https://race", content_type, "job")
                )
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(content_type,))
            for content_type in content_types
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        digest = results[0][0]
        self.assertTrue(all(result[0] == digest for result in results))
        self.assertEqual({result[1] for result in results}, {"new", "exists"})
        byte_files = [
            path
            for path in self.kb.sources.glob(f"{digest}.*")
            if path.suffix != ".toml"
        ]
        self.assertEqual(len(byte_files), 1)
        self.assertEqual(byte_files[0].read_bytes(), data)
        sidecars = list(self.kb.sources.glob(f"{digest}.toml"))
        self.assertEqual(len(sidecars), 1)

    def test_byte_file_present_sidecar_missing_repairs_orphan(self) -> None:
        data = b"crash orphan: bytes with no sidecar"
        digest = hashlib.sha256(data).hexdigest()
        orphan = self.kb.sources / f"{digest}.md"
        orphan.write_bytes(data)

        digest2, status = store(
            self.kb, data, "https://x/repair", "text/markdown", "job-repair"
        )

        self.assertEqual(digest2, digest)
        self.assertEqual(status, "new")
        self.assertEqual(orphan.read_bytes(), data)
        provenance = read_provenance(self.kb, digest)
        self.assertEqual(provenance["job"], "job-repair")

    def test_only_pair_survives_in_sources(self) -> None:
        digest, _status = store(
            self.kb, b"clean directory payload", "https://x", "text/markdown", "job"
        )
        names = sorted(path.name for path in self.kb.sources.iterdir())
        self.assertEqual(names, sorted([f"{digest}.md", f"{digest}.toml"]))

    def test_stored_urls_is_empty_over_an_empty_sources_dir(self) -> None:
        self.assertEqual(stored_urls(self.kb), set())

    def test_stored_urls_collects_every_sidecar_url(self) -> None:
        store(self.kb, b"payload one", "https://x/one", "text/plain", "job")
        store(self.kb, b"payload two", "https://x/two", "text/plain", "job")
        self.assertEqual(stored_urls(self.kb), {"https://x/one", "https://x/two"})

    def test_stored_urls_skips_a_sidecar_that_fails_to_parse(self) -> None:
        store(self.kb, b"payload one", "https://x/one", "text/plain", "job")
        junk = self.kb.sources / "notadigest.toml"
        junk.write_text("this is not valid toml [[[", encoding="utf-8")
        self.assertEqual(stored_urls(self.kb), {"https://x/one"})


if __name__ == "__main__":
    unittest.main()
