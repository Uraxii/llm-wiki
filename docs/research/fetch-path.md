# Fetch path: URL to clean markdown for `kb ingest`

Ticket agent-kb-0zf.7. Question: reuse the old `kb clip` or adopt an off-the-shelf extractor?
Every licence below was read from the repo's own licence file, not PyPI metadata. Dates are last release as of 2026-08-29.

| tool | licence (verified file) | deps | HTML article | github | PDF | redirects | JS pages | phone-home | maintenance | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| trafilatura | Apache-2.0 (`LICENSE`, also in wheel `dist-info/licenses/LICENSE`) | 7 (lxml, justext, courlan, htmldate, charset_normalizer, urllib3, certifi) | yes, best-in-class boilerplate removal, native markdown output | partial, generic heuristics, untested on GitHub chrome | no | yes (built-in fetcher) | no | no | 2.2.0, 2026-07-31; py3.14 classifier | **Primary pick.** One pip line gives extraction plus markdown |
| readability-lxml | Apache-2.0 (`LICENSE`) | 3 (lxml, chardet, cssselect) | yes | partial | no | n/a, bring your own fetcher | no | no | 0.9, 2026-08-27; `requires_python <3.15` | Lightest; what `kb clip` already uses. Runner-up, needs its own HTML to markdown step |
| markitdown | MIT (`LICENSE`) | 6 core incl. magika (ONNX runtime); PDF behind `[pdf]` extra | partial, converts the whole page, does not isolate content | partial | yes, via pdfminer.six + pdfplumber + Pillow + pypdfium2 | yes (requests) | no | no by default; opt-in billable Azure Document Intelligence path exists | 0.1.7, 2026-07-29; no 3.14 classifier, deps declare 3.14 | Format converter, not an extractor. Heavy. Skip |
| defuddle | MIT (`LICENSE`, JS package) | Node runtime + jsdom or linkedom | yes (JS) | partial | no | n/a | no | no | npm 0.19.3, 2026-08-22 | **No Python port exists** (`defuddle`, `defuddle-py`, `py-defuddle` all 404 on PyPI). Would need a Node subprocess. Skip |
| html2text + stdlib urllib | GPL-3.0 (`COPYING`, no `LICENSE` file) | 1 (html2text has zero deps) | no, whole-page conversion, nav included | no | no | yes (urllib default) | no | no | 2025.4.15, 16 months stale; no 3.14 classifier | Copyleft and no extraction. Skip |
| newspaper4k | MIT (`LICENSE`) | 11 (Pillow, feedparser, tldextract, requests, ...) | yes, tuned for news bylines | no | no | yes (requests) | no | no | 0.9.6, 2026-07-19; pyproject branches on 3.14 | News-only heuristics, heaviest install. Skip |
| monolith | CC0-1.0 (`LICENSE`), **not OSI-approved** | Rust binary (cargo or package manager) | no, archives the full page with data-URI assets | partial, snapshot only | no | yes (reqwest) | no | no | v2.10.1, 2025-03-30 | Archiver, not extractor. Fails licence constraint. Skip |
| curl + pandoc | curl: curl licence (`COPYING`), **not OSI-listed**, review pending. pandoc: GPL-2.0-or-later (`COPYRIGHT`) | 2 system binaries | no, pandoc keeps all structure | no | no, pandoc has no PDF reader | partial, curl needs `-L` | no | no | curl 8.21.0, 2026-06-24; pandoc 3.11, 2026-08-29 | No content extraction. Fine as a debugging baseline, not the tool |
| `kb clip` (`scripts/kb-clip.py`) | n/a, in-house | 2 (readability-lxml, lxml) | yes, readability then densest-block fallback | no, times out or crashes | no, no Content-Type gate | yes, with SSRF re-check per hop | no | no | in-house | Keep the fetch guards, drop the extractor and path code |

PDF, evaluated separately:

| tool | licence (verified file) | deps | notes | maintenance | verdict |
|---|---|---|---|---|---|
| pypdf | BSD-3-Clause (`LICENSE`) | 0 on Python >= 3.11 | Fine on text-layer PDFs; scrambles multi-column layouts; no OCR | 6.16.2, 2026-08-23; 3.14 classifier | **PDF primary** |
| pdfminer.six | MIT (`LICENSE`) | 2 (charset-normalizer, cryptography) | Better reading order on layout-heavy PDFs; no OCR | 20260107, 2026-01-07; 3.14 classifier | Fallback if pypdf output is garbled |
| markitdown `[pdf]` | MIT | pdfminer.six + pdfplumber + Pillow + pypdfium2 | Same engine as pdfminer.six plus table heuristics | 0.1.7, 2026-07-29 | Skip, four deps for the same text |

## Recommendation

Adopt **trafilatura** as the HTML extractor. Fetch with stdlib `urllib` (reusing `kb clip`'s guards, see below), then call `trafilatura.extract(html, url=url, output_format="markdown")`. Fallback chain:

1. Response is HTML: trafilatura. If it returns `None` or empty, fall back to the densest `<article>`/`<main>`/`<body>` block, ported from `kb clip`.
2. Response is `application/pdf` (by Content-Type or `%PDF` magic): pypdf `extract_text()` per page. If the result is empty or garbled, pdfminer.six `extract_text()` as a second try only if that dependency is later added.
3. Anything else, or every extractor failed: store the raw bytes with the response headers and mark the note as unextracted. Never drop the fetch.

Against the hard constraints: Apache-2.0 verified from the LICENSE file in the repo and in the downloaded wheel (`pip download trafilatura --no-deps`, 2.2.0). Pure library, no server, no external API calls. Free. One `pip install trafilatura` line, seven transitive deps, all pure Python except lxml which ships cp314 wheels. Python 3.14 is in its classifiers. readability-lxml is lighter but only returns cleaned HTML, so it would need a second package (or `kb clip`'s block-only `html_to_markdown`) to produce markdown, and trafilatura's own benchmarks put readability behind it on precision. The extra deps buy native markdown output and a maintained extractor that already handles redirects and encoding.

Do the fetch ourselves rather than with `trafilatura.fetch_url`, so timeouts, 403 handling, User-Agent, tracking-param stripping, and the SSRF guard stay in our code and one place.

Headless browser (Playwright) for JS-rendered pages is **out of scope** for this ticket. It pulls in full browser binaries, needs a driver process, and most ingest targets (articles, docs, GitHub) serve usable static HTML. Note it as a future fallback: when step 1 yields near-empty text on a page that returned 200, log it as `js-rendered?` so we can measure how often it happens before paying for Playwright. Paywalled pages get the same treatment: extract the teaser, flag it, move on.

GitHub URLs: do not scrape the HTML at all. Rewrite `github.com/{o}/{r}/blob/{ref}/{path}` to `raw.githubusercontent.com/{o}/{r}/{ref}/{path}` and fetch the raw file. That sidesteps the timeout `kb clip` hits and gives exact source text. Repo root and issue pages remain generic-HTML cases.

## kb clip

Source: `~/Projects/knowledgebase/scripts/kb-clip.py` (504 lines). `scripts/kb-svc.py` `/clip` route (`kb_clip_and_atomize` to `_clip_url`) is a thin wrapper over the same `clip()`.

Keep, port as-is:

- `check_url_scheme()` and `check_destination_is_public()` (lines 86-117), plus `_DestinationCheckingRedirectHandler` (120-139), which re-runs the public-address check on every redirect hop. Good SSRF guard; the DNS-rebinding gap is documented at 206-211 and acceptable.
- `decode_html()` cascade (158-195): BOM, strict UTF-8, `<meta charset>` sniff, header charset, lossy UTF-8 with a warning. Bug-driven, correct order. trafilatura does its own detection but we still need decoded text for the fallback path.
- `parse_metadata()` (275-311): OG, then Twitter, then JSON-LD, then plain `<meta>`/`<title>`/`<time>`. Cheap and useful for front matter.
- `build_note_path()` (388-407): `O_CREAT|O_EXCL` atomic filename reservation. Keep the mechanism, fix the input (below).
- `pick_densest_container()` (343-348) as the last-resort HTML fallback when trafilatura returns nothing.

Drop:

- `slugify()` (382-385) has no length cap, and `build_note_path()` puts the slug straight into `sources_dir / f"{slug}.md"` then `os.open()` at line 402, which raises `OSError` ENAMETOOLONG on long titles. Cap the slug at about 80 characters and add a short hash suffix.
- `FETCH_TIMEOUT_SEC = 20` (line 45) is the only timeout, applied at `fetch_html()` line 221, with no retry. The parse side has no bound at all: `clip()` calls `lxml.html.fromstring` (472) and `extract_body_markdown()` calls `readability.Document(html).summary()` (363) on GitHub's huge hydrated DOM. That parse is the likely "GitHub timeout". The raw-URL rewrite above removes the case; a size cap on the response body covers the rest.
- No Content-Type check in `fetch_html()` (198-228): a PDF or image goes straight into `lxml.html.fromstring`. Gate on Content-Type and magic bytes before choosing an extractor.
- `HTTPError` (403, 404, 5xx) propagates uncaught. Catch it, record status, keep the note.
- `html_to_markdown()` (314-341) and `extract_body_markdown()` (351-379): block-only markdown, no links or inline formatting, readability dependency. Replaced by trafilatura.
- No tracking-param stripping exists (grep for `utm` finds nothing); build it fresh (drop `utm_*`, `fbclid`, `gclid`, `ref`).

## Sources

- https://raw.githubusercontent.com/adbar/trafilatura/master/LICENSE
- https://raw.githubusercontent.com/adbar/trafilatura/master/README.md
- https://raw.githubusercontent.com/adbar/trafilatura/master/trafilatura/downloads.py
- https://pypi.org/pypi/trafilatura/json
- https://raw.githubusercontent.com/buriy/python-readability/master/LICENSE
- https://raw.githubusercontent.com/buriy/python-readability/master/README.md
- https://pypi.org/pypi/readability-lxml/json
- https://raw.githubusercontent.com/microsoft/markitdown/main/LICENSE
- https://raw.githubusercontent.com/microsoft/markitdown/main/README.md
- https://github.com/microsoft/markitdown/blob/main/packages/markitdown/src/markitdown/converters/_pdf_converter.py
- https://github.com/microsoft/markitdown/blob/main/packages/markitdown/pyproject.toml
- https://github.com/microsoft/markitdown/releases/tag/v0.1.7
- https://pypi.org/pypi/markitdown/json
- https://pypi.org/pypi/magika/json
- https://pypi.org/pypi/onnxruntime/json
- https://raw.githubusercontent.com/kepano/defuddle/main/LICENSE
- https://raw.githubusercontent.com/kepano/defuddle/main/package.json
- https://raw.githubusercontent.com/kepano/defuddle/main/README.md
- https://registry.npmjs.org/defuddle
- https://api.github.com/search/repositories?q=defuddle+python
- https://pypi.org/pypi/defuddle/json (404), https://pypi.org/pypi/defuddle-py/json (404), https://pypi.org/pypi/py-defuddle/json (404)
- https://raw.githubusercontent.com/Alir3z4/html2text/master/COPYING
- https://raw.githubusercontent.com/Alir3z4/html2text/master/README.md
- https://pypi.org/pypi/html2text/json
- https://pypi.org/pypi/lxml/json
- https://github.com/AndyTheFactory/newspaper4k/blob/master/LICENSE
- https://github.com/AndyTheFactory/newspaper4k/blob/master/pyproject.toml
- https://github.com/AndyTheFactory/newspaper4k/releases
- https://github.com/Y2Z/monolith/blob/main/LICENSE
- https://github.com/Y2Z/monolith/blob/main/Cargo.toml
- https://github.com/Y2Z/monolith/blob/main/README.md
- https://github.com/Y2Z/monolith/releases
- https://github.com/curl/curl/blob/master/COPYING
- https://curl.se/docs/copyright.html
- https://curl.se/mail/lib-2026-02/0001.html
- https://spdx.org/licenses/curl.html
- https://github.com/curl/curl/releases/tag/curl-8_21_0
- https://github.com/jgm/pandoc/blob/main/COPYRIGHT
- https://github.com/jgm/pandoc/releases/tag/3.11
- https://pandoc.org/MANUAL.html
- https://github.com/py-pdf/pypdf/blob/main/LICENSE
- https://github.com/py-pdf/pypdf/blob/main/pyproject.toml
- https://github.com/py-pdf/pypdf/releases/tag/6.16.2
- https://github.com/pdfminer/pdfminer.six/blob/master/LICENSE
- https://github.com/pdfminer/pdfminer.six/blob/master/pyproject.toml
- https://github.com/pdfminer/pdfminer.six/releases/tag/20260107
- https://github.com/jsvine/pdfplumber/blob/main/requirements.txt
- https://playwright.dev/python/docs/library
- Local: `pip download trafilatura --no-deps` (2.2.0 wheel, `dist-info/licenses/LICENSE` is Apache-2.0, `License-Expression: Apache-2.0`, classifier `Python :: 3.14`)
- Local: `~/Projects/knowledgebase/scripts/kb-clip.py`, `scripts/kb-svc.py` lines 470-510 and 1015-1032
