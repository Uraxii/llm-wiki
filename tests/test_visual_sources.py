"""Phase 15: an image or PDF source becomes an ordinary summary page,
against the fake endpoint. A few-hundred-byte PNG and a small PDF are
built by hand below, never downloaded and never larger than ~2KB.
"""

import hashlib
import io
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "llm-wiki"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from llmwiki.core import Kb, parse_frontmatter  # noqa: E402
from llmwiki.lint import lint_pages  # noqa: E402
from llmwiki import summarize  # noqa: E402
from fake_endpoint import FakeEndpoint  # noqa: E402
from kb_config import config_toml  # noqa: E402


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def tiny_png() -> bytes:
    """A valid 1x1 RGB PNG, built by hand: signature, IHDR, one
    zlib-compressed scanline, IEND. About 70 bytes."""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    scanline = b"\x00" + b"\xff\x00\x00"  # filter byte + one red pixel
    idat = zlib.compress(scanline)
    return (
        signature
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def tiny_pdf() -> bytes:
    """A minimal one-page PDF with a correct xref table, built by hand.
    A few hundred bytes."""
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 3 3] >>\nendobj\n",
    ]
    body = b"%PDF-1.4\n"
    offsets = []
    for obj in objects:
        offsets.append(len(body))
        body += obj
    xref_offset = len(body)
    xref = f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        xref += f"{offset:010d} 00000 n \n".encode()
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF"
    ).encode()
    return body + xref + trailer


def _run_quiet(root, digests):
    with redirect_stdout(io.StringIO()):
        return summarize.run(root, digests)


GOOD_REPLY = (
    "---\n"
    "title: A Photographed Chart\n"
    "identifiers: []\n"
    "---\n\n"
    "A short abstract of what the image depicts.\n"
)


class VisualSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / ".kb"
        for sub in ("wiki", "sources"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "log.md").write_text("# log\n")

    def _write_config(self, url: str, pdf_part: str | None = None) -> None:
        (self.root / "config.toml").write_text(
            config_toml(url, {"summarize": "cheap"}, pdf_part=pdf_part)
        )

    def _store(self, data: bytes, content_type: str) -> str:
        digest = hashlib.sha256(data).hexdigest()
        ext = {
            "image/png": ".png",
            "application/pdf": ".pdf",
        }.get(content_type, ".bin")
        (self.root / "sources" / f"{digest}{ext}").write_bytes(data)
        (self.root / "sources" / f"{digest}.toml").write_text(
            'url = "https://example.com/visual"\n'
            'fetched = "2024-01-01T00:00:00Z"\n'
            f'content_type = "{content_type}"\n'
            'job = "manual"\n'
        )
        return digest

    def test_png_source_summarizes_into_a_lint_clean_page(self) -> None:
        digest = self._store(tiny_png(), "image/png")

        def respond(_path: str, body: dict) -> dict:
            return {"choices": [{"message": {"content": GOOD_REPLY}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 0)
        pages = list((self.root / "wiki").glob("*.md"))
        self.assertEqual(len(pages), 1)
        fields, _body = parse_frontmatter(pages[0].read_text())
        self.assertEqual(fields["kind"], "summary")
        self.assertEqual(fields["source"], digest)
        self.assertEqual(lint_pages(self.root, [pages[0]]), [])

    def test_png_request_body_carries_an_image_url_data_part(self) -> None:
        digest = self._store(tiny_png(), "image/png")
        captured = {}

        def respond(_path: str, body: dict) -> dict:
            captured["body"] = body
            return {"choices": [{"message": {"content": GOOD_REPLY}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            _run_quiet(self.root, [digest])

        content = captured["body"]["messages"][0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(
            content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        )

    def test_pdf_source_summarizes_under_pdf_part_file(self) -> None:
        digest = self._store(tiny_pdf(), "application/pdf")

        def respond(_path: str, body: dict) -> dict:
            return {"choices": [{"message": {"content": GOOD_REPLY}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url, pdf_part="file")
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 0)
        pages = list((self.root / "wiki").glob("*.md"))
        self.assertEqual(len(pages), 1)
        self.assertEqual(lint_pages(self.root, [pages[0]]), [])

    def test_pdf_part_none_drops_pdf_naming_the_setting(self) -> None:
        digest = self._store(tiny_pdf(), "application/pdf")

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "unused"}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url, pdf_part="none")
            code = _run_quiet(self.root, [digest])
            self.assertEqual(fake.requests, [])

        self.assertEqual(code, 1)
        self.assertEqual(list((self.root / "wiki").glob("*.md")), [])
        log = (self.root / "log.md").read_text()
        self.assertIn(digest, log)
        self.assertIn("pdf_part", log)

    def test_over_cap_attachment_raises_naming_size_and_cap(self) -> None:
        from llmwiki.model import MAX_ATTACHMENT_BYTES

        data = tiny_png() + b"\x00" * MAX_ATTACHMENT_BYTES
        digest = self._store(data, "image/png")

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "unused"}}]}

        err = io.StringIO()
        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            with redirect_stderr(err):
                code = _run_quiet(self.root, [digest])
            self.assertEqual(fake.requests, [])

        self.assertEqual(code, 1)
        message = err.getvalue()
        self.assertIn(str(len(data)), message)
        self.assertIn(str(MAX_ATTACHMENT_BYTES), message)
        self.assertTrue((self.root / "sources" / f"{digest}.png").exists())

    def test_a_content_type_that_is_neither_image_nor_pdf_goes_as_text(
        self,
    ) -> None:
        """The content type decides image or PDF and nothing else. Bytes
        that are not one of those and decode as UTF-8 are text, whatever
        the sidecar calls them."""
        digest = self._store(b"plain words", "application/octet-stream")
        sent = []

        def respond(_path: str, body: dict) -> dict:
            sent.append(body)
            return {"choices": [{"message": {"content": GOOD_REPLY}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("plain words", sent[0]["messages"][0]["content"])
        self.assertEqual(len(list((self.root / "wiki").glob("*.md"))), 1)

    def test_truncated_text_file_drops_as_decode_failure_distinguishably(
        self,
    ) -> None:
        digest = self._store(b"\xff\xfe not valid utf-8", "text/markdown")

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "unused"}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code = _run_quiet(self.root, [digest])
            self.assertEqual(fake.requests, [])

        self.assertEqual(code, 1)
        log = (self.root / "log.md").read_text()
        self.assertIn(digest, log)
        self.assertIn("cannot decode source", log)

    def test_summarize_image_model_used_when_set(self) -> None:
        digest = self._store(tiny_png(), "image/png")
        captured = {}

        def respond(_path: str, body: dict) -> dict:
            captured["model"] = body["model"]
            return {"choices": [{"message": {"content": GOOD_REPLY}}]}

        with FakeEndpoint(respond) as fake:
            (self.root / "config.toml").write_text(
                config_toml(
                    fake.url,
                    {"summarize": "cheap", "summarize_image": "vision-model"},
                )
            )
            _run_quiet(self.root, [digest])

        self.assertEqual(captured["model"], "vision-model")

    def test_summarize_model_used_when_summarize_image_unset(self) -> None:
        digest = self._store(tiny_png(), "image/png")
        captured = {}

        def respond(_path: str, body: dict) -> dict:
            captured["model"] = body["model"]
            return {"choices": [{"message": {"content": GOOD_REPLY}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            _run_quiet(self.root, [digest])

        self.assertEqual(captured["model"], "cheap")

    def test_unrecognized_pdf_part_is_a_startup_visible_error(self) -> None:
        digest = self._store(tiny_pdf(), "application/pdf")

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "unused"}}]}

        err = io.StringIO()
        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url, pdf_part="carrier-pigeon")
            with redirect_stderr(err):
                code = _run_quiet(self.root, [digest])
            self.assertEqual(fake.requests, [])

        self.assertEqual(code, 1)
        self.assertIn("pdf_part", err.getvalue())

    def test_rerun_over_up_to_date_image_page_makes_zero_model_calls(self) -> None:
        digest = self._store(tiny_png(), "image/png")

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": GOOD_REPLY}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            first = _run_quiet(self.root, [digest])
            self.assertEqual(len(fake.requests), 1)
            second = _run_quiet(self.root, [digest])
            self.assertEqual(len(fake.requests), 1)

        self.assertEqual((first, second), (0, 0))

    def test_resummarizing_image_with_story_field_keeps_it(self) -> None:
        digest = self._store(tiny_png(), "image/png")

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": GOOD_REPLY}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            _run_quiet(self.root, [digest])
            pages = list((self.root / "wiki").glob("*.md"))
            self.assertEqual(len(pages), 1)
            page_path = pages[0]
            fields, body = parse_frontmatter(page_path.read_text())
            fields["story"] = "some-story-slug"
            from llmwiki.core import render_frontmatter

            page_path.write_text(render_frontmatter(fields, body))

            # Force a second call by changing the prompt prefix.
            (self.root / "SUMMARIZE.md").write_text("Extra note.\n")
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 0)
        fields, _body = parse_frontmatter(page_path.read_text())
        self.assertEqual(fields["story"], "some-story-slug")


if __name__ == "__main__":
    unittest.main()
