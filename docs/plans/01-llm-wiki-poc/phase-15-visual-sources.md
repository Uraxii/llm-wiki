[overview](overview.md)

# Phase 15: image and PDF sources

**Goal.** An image or a PDF stored in `sources/` becomes an ordinary summary
page, written by a vision model, so that `dedup`, `embed`, `search`, and `lint`
need no change at all.

**Problem.** A source the CLI cannot decode as UTF-8 is stored and then never
summarized. `sources.store` keeps the bytes and writes the provenance sidecar,
so the source exists and is real. `summarize._process_digest` then calls
`.read_bytes().decode("utf-8")` and drops the digest on `UnicodeDecodeError`,
logging "cannot read source". Nothing retries it, because nothing knows it is
retryable.

The observable symptom is a source that never leaves the backlog.
`vectors._unsummarized` (`vectors.py:226-230`) lists every stored digest with
no summary page, so `llmwiki status` reports the image forever and `search`
prints its "N sources without a summary" warning forever. PDFs are already in
this state today: `sources.EXTENSIONS` maps `application/pdf` to `.pdf`
(`sources.py:17`), so a stored PDF is a first-class source that no code path
can ever summarize.

**Why this parses nothing, now that it could.** The CLI's dependency rule is
gone by user directive. Any library is available, `pypdf` included, and this
phase still uses none, on the user's call.

The reason is what a text extractor cannot see. It returns the text layer, and
a scanned page, a photographed document, a chart, a diagram, and a table laid
out in whitespace all have no text layer or a misleading one. The extractor
returns an empty string and the run looks like a success, which is the silent
failure this repo keeps getting burned by. A vision model reads the page.

So the ordering is: hand the raw bytes to the model, and reach for `pypdf` only
if the step 0 probe shows the endpoint refuses PDF parts. That fallback is now
available without another conversation, which is the whole reason the rule was
relaxed here.

## What does not change, and why that is the whole point

A summary page written from an image is a `kind: summary` page like any other:
same frontmatter keys, same `source:` digest, same identifiers, same body. So:

- `lint`'s seven checks are all frontmatter checks. `_check_dangling_source`
  (`lint.py:110-119`) asks only whether the `source:` digest is present in
  `sources/`, which it is. No check reads a source's bytes or its type.
- `dedup` joins on identifiers and vectors over the summary text, not the
  source.
- `embed` and `search` embed the summary page. There is no image embedding
  here, and no second vector space to keep calibrated. `NEIGHBOUR_FLOOR`
  (`vectors.py:47`) keeps its meaning.
- Phase 14's commit path is unchanged. The lock window, the live
  `_summary_index` re-read, and `_carry_story` all sit after the model call and
  do not care what the model was shown.

That containment is the design. If a change to this phase starts touching
`dedup` or `vectors`, the shape is wrong.

## Step 0: measure the wire format before writing any of it

**This phase does not start until this is answered, and the answer is a
recorded measurement, not a citation.**

`model._post` targets an OpenAI-compatible endpoint whose url comes from
`[providers.<name>].url`, which `model.resolve_target` reads into
`ModelTarget.url` (`model.py:149-178`).
"OpenAI-compatible" is a claim about `/chat/completions` and `/embeddings`
with text. Attachment support varies by
server, and it varies differently for images and for PDFs.

- Images as a data URL in an `image_url` content part are widely supported.
  Expect this to work and confirm it anyway.
- PDFs have no single accepted shape. Some servers take a `file` part, some
  take a document part under another name, and some accept neither and answer
  400. This is the risk the user accepted when choosing to cover PDFs, and it
  is cheaper to find out in a probe than in a summarizer.

Write a throwaway probe that posts one tiny image and one tiny PDF to the
configured endpoint and records the exact request body that worked. Keep it out
of `llmwiki/` and out of `tests/`. Record the result as a `decisions.tsv` row
carrying the working body shape and the model name it worked with.

**If PDF parts are refused,** the image half ships on its own and PDFs fall
back to `pypdf` text extraction into the existing text path. Say plainly, in
the report and in a `decisions.tsv` row, that this fallback is blind to scans
and charts, and that a PDF with no text layer will summarize to nothing. That
is a real loss of coverage, not a neutral substitution.

Prefer `pypdf`, which is enough for a text layer. A rasterizing library is
permitted now that the dependency rule is dropped, but it only helps if the
pages then go to the vision model, and the probe refusing PDF parts is exactly
the case where that is unavailable. Do not add one to serve a path that cannot
run.

## The four changes

**1. `sources` learns the image types.** `EXTENSIONS` (`sources.py:17`) maps
`text/markdown` and `application/pdf` and falls back to `.txt` for everything
else, so an image stored today lands as `<digest>.txt`. That is only a naming
defect, since the bytes are intact and `_source_path` globs `{digest}.*`, but a
`.txt` file holding PNG bytes is a trap for the next reader.

Add `image/png`, `image/jpeg`, `image/webp`, and `image/gif`. The provenance
sidecar already records the real `content_type` (`sources.py:60`), and that
sidecar, not the file extension, is what the summarizer reads to decide.

**2. `model.chat` learns to carry an attachment.**

```python
def chat(
    config: dict,
    step: str,
    prompt: str,
    model: str | None = None,
    attachment: tuple[str, bytes] | None = None,
) -> str:
    """One chat completion for pipeline `step`. `attachment` is
    (media_type, raw bytes); when given, `content` becomes the content
    part list the probe in step 0 proved, with the bytes base64 encoded
    as a data URL, instead of the plain string form."""
```

Keep the plain string body for a text call, byte for byte. Every existing
caller passes no attachment and must produce an identical request to the one it
produces today. This is what keeps the existing suite meaningful as a
regression gate on this change.

`ModelError` stays the entire error contract. A refused attachment is a
`ModelError` like any other endpoint failure.

**3. `summarize` branches on the source's media type, not on a decode failure.**

Today the branch is an exception handler: try to decode, drop on failure. That
conflates three different things, which is why an image is indistinguishable
from a truncated file. Read `content_type` from the provenance sidecar and
decide up front.

```
text type      -> decode, prompt = prefix + SOURCE_DELIMITER + text   (today's path)
visual type    -> prompt = prefix + SOURCE_ATTACHMENT_NOTE, attachment = (type, bytes)
unknown type   -> drop with a reason that names the type, as today
decode failure -> still a drop, and now it means a genuinely broken text file
```

`SOURCE_DELIMITER` is `"\n\n=== SOURCE TEXT FOLLOWS ===\n\n"`
(`summarize.py:79`). A visual source needs its own sibling constant, because
"SOURCE TEXT FOLLOWS" is a lie when the source is a chart, and the model reply
quality depends on the prompt being true.

The ordering rule from phase 14 is unchanged and still binding: everything up
to and including the paid `chat` call happens with no lock, and the commit
window opens only once there is content to write.

**The prompt fingerprint must cover the change.** `prompt_fingerprint` decides
what `run` re-summarizes (`summarize.py:275`). A source summarized under the
text path and later reachable under the visual path must re-summarize, so the
fingerprint has to change when the visual prompt does. Fold the visual note
into the fingerprint input, not just the text prefix.

**4. `[models] summarize_image`, falling back to `[models] summarize`.** The
standing directive is that every model for a job is configurable in the toml
file. A vision-capable model is usually a different, more expensive model than
the one summarizing text, and forcing one entry to be both makes every text
summary pay vision prices. The fallback keeps a config that never names it
working unchanged.

## A size cap, because a JSON body is not a stream

Base64 inflates by roughly a third, so a 40MB scan becomes a 53MB request body
built entirely in memory. Cap the attachment at a constant in `model.py` and
raise `ModelError` above it, naming the size and the cap. Set the constant from
the probe in step 0: use the largest attachment the endpoint actually accepted,
rounded down, rather than a number chosen because it looks round.

A source over the cap is a drop with a logged reason, not a crash, and the
source stays in `sources/` where a later run with a raised cap will find it.

## What is deliberately out

- **Image embedding and a second vector space.** The summary text is what gets
  embedded. A second space would need its own calibrated floor and its own
  measurement, and it buys nothing until a query wants to find an image by
  visual similarity rather than by what it depicts.
- **Rasterizing PDF pages.** That needs a library. See step 0.
- **Fetching images over the network.** `fetch.ACCEPTED_TYPES`
  (`fetch.py:26-28`) is
  four text types, and this phase does not widen it. Images and PDFs arrive as
  local paths. Widening the accept list is a separate change with its own size
  cap and its own audit of the redirect rules, and it is not needed to prove
  this phase works.
- **OCR.** The vision model reads the image. There is no second text layer.

## Verification

Observed on real runs, never argued. Each gate is seen failing before its code
exists. A green suite is not evidence here: this repo has had passing tests
hide a real defect in three consecutive phases.

**Step 0 gate, before anything else.**

- The probe's working request body for an image is recorded, with the model
  name, in `decisions.tsv`.
- The same for a PDF, or an explicit recorded finding that the endpoint refuses
  PDF parts, with the status and reason.

**Regression, that the text path did not move.**

- The full suite passes unmodified, and stays identical under the dead-proxy
  run.
- A text summarize produces a request body byte-identical to the one on the
  previous commit. Capture both through `tests/fake_endpoint.py` and diff. This
  is the gate that makes every other existing test trustworthy.

**The image path.**

- A stored PNG becomes a summary page that `lint` passes, driven end to end.
- That page is embedded by `embed` and returned by `search` for a query about
  what the image depicts. Nothing downstream was changed, so this proves the
  containment claim rather than the ranking.
- `llmwiki status` no longer lists the digest, which is the symptom this phase
  exists to remove. Observe it listed before and absent after.
- A JPEG, a WebP, and a GIF each store with the right extension, and the
  provenance sidecar carries the real `content_type`.

**The PDF path**, if step 0 cleared it.

- A stored PDF becomes a summary page that `lint` passes.
- A PDF stored before this phase, already sitting in `sources/` with no summary,
  is picked up by a plain `llmwiki summarize` with no re-ingest. Backlog
  recovery is the point: the user's existing PDFs are the test corpus.

**Failure paths, each driven once.**

- An attachment over the size cap drops with a reason naming the size and the
  cap, exits nonzero, and leaves the source in `sources/`.
- An endpoint that refuses the attachment surfaces as a `ModelError` with no
  request detail and no credential in the message, and destroys no existing
  page.
- A truncated text file still drops as a decode failure, and its reason is
  distinguishable from an unsupported type.
- A source whose provenance sidecar names a type the phase does not handle
  drops with the type in the reason.
- Re-summarizing an image source that already carries a `story:` keeps that
  field. This is `_carry_story` from phase 14 and it must not regress on the
  new path.

**Concurrency, unchanged but not assumed.**

- Two processes summarizing the same image digest produce exactly one summary
  page. Same reproducer shape as phase 14, real subprocesses, never threads.

## What landed

The four changes shipped, plus the probe script, against `.venv/bin/python`
only. `MAX_ATTACHMENT_BYTES` still ships as the labelled guess D4 describes,
not a measurement: step 0 tested which content-part shapes the endpoint
accepts, never how large an attachment it accepts. See `decisions.tsv` for the
specific calls made while implementing.

- `sources.EXTENSIONS` gained the four image types.
- `model.chat` gained `attachment`, byte-identical for the no-attachment
  case (diffed against a captured pre-change body), plus
  `MAX_ATTACHMENT_BYTES` and the D1/D2 content-part shapes.
- `summarize` branches on provenance `content_type` via a `_content_kind`
  helper reusing `fetch.ACCEPTED_TYPES`, with `SOURCE_ATTACHMENT_NOTE` as
  the visual sibling of `SOURCE_DELIMITER`. `_content_kind` was deleted in
  `37497be`: reusing a network allowlist as a content gate dropped every
  stored `.xml`, `.json` and `.py` source as `inert`. The content type now
  selects the attachment branch and nothing else, and text is whatever
  decodes as UTF-8 under `MAX_SOURCE_TEXT_BYTES`. The two prompt fingerprints
  this phase introduced were later collapsed back into one in `cbdc38c`,
  because both derived from the same prefix, so nothing at runtime could
  move one without moving the other.
- `[models] summarize_image` falls back to `[models] summarize` through
  `_image_target`, no parallel lookup.
- `.nikki-agents/probe-attachments.py` exists and has now been run
  (2026-09-01, `https://openrouter.ai/api/v1`). It confirms the D2 default:
  `google/gemini-2.5-flash` accepted all three shapes, `openai/gpt-4o-mini`
  accepted the image as `image_url` and the PDF as `file`/`file_data` but
  refused the PDF as `image_url` with HTTP 400 `invalid_image_format`. Both
  models read the word PROBE back out of the PDF, so the part was parsed and
  not merely accepted. `pdf_part` keeps its `"file"` default, `"image_url"`
  stays a real but provider-specific option, and the `pypdf` fallback
  condition is not met.

Since driven: the phase-14 two-process concurrency reproducer against a
visual source specifically. `.nikki-agents/repro-visual-lock.py` races two
real OS subprocesses on one kb. It was proven RED first, 9 corruptions in 9
runs against a lock-disabled copy in a tempdir, then GREEN 15 of 15 against
the real code across image/image, pdf/pdf, and image/pdf. The lock holding
here is now an observation, not an argument from the diff.
