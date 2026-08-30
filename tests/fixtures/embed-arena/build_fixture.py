#!/usr/bin/env python3
"""Build the embed-arena fixture: ~60 Wikipedia articles across two domains,
summarized into llm-wiki "summary page" markdown by deepseek/deepseek-v3.2.

Writes tests/fixtures/embed-arena/pages/<slug>.md and manifest.json next to
this script. Caches fetched source text and raw API responses under
/tmp/arena-embed/ so a rerun with warm caches makes zero paid calls.

Run:
  export LLM_WIKI_API_KEY=...
  python3 build_fixture.py
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGES_DIR = HERE / "pages"
MANIFEST_PATH = HERE / "manifest.json"
SOURCE_CACHE = Path("/tmp/arena-embed/source")
SUMMARY_CACHE = Path("/tmp/arena-embed/summaries")

WIKI_UA = "agent-kb-embed-arena-fixture/1.0 (local dev fixture builder for a retrieval eval, no email)"
MODEL = "deepseek/deepseek-v3.2"
API_URL = "https://openrouter.ai/api/v1/chat/completions"

LONG_CHARS = 6000
SHORT_CHARS = 600
SPEND_CAP_USD = 0.50
EST_COST_PER_CALL_USD = 0.003  # rough deepseek-v3.2 ceiling for a short prompt

COOKING = [
    "Maillard reaction", "Sourdough", "Braising", "Sous vide", "Emulsion (cooking)",
    "Mirepoix", "Tempering (chocolate)", "Gluten", "Yeast", "Roux",
    "Fermentation in food processing", "Knife sharpening", "Pressure cooking",
    "Stock (food)", "Caramelization", "Baking powder", "Umami", "Marination",
    "Deep frying", "Confit", "Pasteurization", "Curing (food preservation)",
    "Ceviche", "Risotto", "Kimchi", "Miso", "Tofu", "Espresso", "Cheesemaking",
    "Smoking (cooking)",
]
SECURITY = [
    "Cross-site scripting", "SQL injection", "Cross-site request forgery",
    "Server-side request forgery", "Clickjacking", "Content Security Policy",
    "Same-origin policy", "HTTP cookie", "JSON Web Token", "OAuth",
    "Transport Layer Security", "Certificate authority",
    "Public key infrastructure", "Password cracking", "Salt (cryptography)",
    "Bcrypt", "Buffer overflow", "Directory traversal attack",
    "Session hijacking", "Privilege escalation", "Denial-of-service attack",
    "Web application firewall", "Penetration test", "Fuzzing",
    "Static application security testing", "Supply chain attack",
    "Zero-day vulnerability", "Common Vulnerabilities and Exposures",
    "Threat model", "Sandbox (computer security)",
]

def build_instructions(short_stub: bool) -> str:
    """The abstract length instruction scales with short_stub so the length
    confound shows up in the written body, not just in how much source text
    the model was fed (deepseek writes an equally fluent 3-to-5 sentence
    abstract from 600 chars as from 6000, so a fixed sentence count alone
    does not produce the confound)."""
    body_len = "1 to 2 sentence" if short_stub else "3 to 5 sentence"
    return f"""You summarize one source document into a wiki knowledge base page.

Return ONLY a markdown page. Start with a YAML frontmatter block delimited
by `---` lines, containing exactly these keys, in this order:
- kind: summary
- title: a short descriptive title for this page
- identifiers: a YAML list of strings copied exactly from the source
  (proper nouns, named techniques, standards, or ids). Write `[]` if none.
- entities: a YAML list of items in the form `kind: value`, one per named
  thing the source is about, for example `technique: braising` or
  `vulnerability: cross-site scripting`.
- claims: a YAML list of items in the form `subject | predicate | object`,
  factual statements found in the source.
- kind_of_source: one word describing the source, for example article.
- action: one sentence on what someone should do or take away from this
  source, or a one sentence statement of its relevance if it implies none.

After the closing `---`, write the body: a {body_len} prose abstract of
the source, and nothing else. No headings, no restating the frontmatter,
no bullet points.

Output nothing before the opening `---` and nothing after the body. Do
not wrap the output in code fences."""


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)


def wiki_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": WIKI_UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def fetch_source(slug: str, title: str) -> str:
    """Cached full plain-text article. page/plain 404s for every title on
    the current REST API, so the primary path is the action API's
    prop=extracts (full plain text); page/summary (intro only, too short
    for the long/short length confound) is the last-resort fallback."""
    cache = SOURCE_CACHE / f"{slug}.txt"
    if cache.exists():
        return cache.read_text(encoding="utf-8")
    text = ""
    try:
        url = ("https://en.wikipedia.org/w/api.php?action=query&format=json"
               "&formatversion=2&prop=extracts&explaintext=1&redirects=1"
               "&titles=" + urllib.parse.quote(title))
        pages = wiki_json(url).get("query", {}).get("pages", [])
        if pages and not pages[0].get("missing"):
            text = pages[0].get("extract", "")
    except (urllib.error.URLError, json.JSONDecodeError):
        pass
    if not text:
        try:
            url = ("https://en.wikipedia.org/api/rest_v1/page/summary/"
                   + urllib.parse.quote(title.replace(" ", "_")))
            text = wiki_json(url).get("extract", "")
        except (urllib.error.URLError, json.JSONDecodeError):
            pass
    if text:
        SOURCE_CACHE.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
    return text


def call_openrouter(api_key: str, slug: str, fingerprint: str, prompt: str) -> tuple:
    """Returns (response_json, was_paid_this_run). Cache key includes the
    prompt fingerprint so an edited prompt invalidates stale cached
    responses instead of silently reusing them."""
    cache = SUMMARY_CACHE / f"{slug}-{fingerprint}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8")), False
    payload = json.dumps({
        "model": MODEL, "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        API_URL, data=payload, method="POST",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.load(resp)
    SUMMARY_CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data), encoding="utf-8")
    return data, True


def strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def split_page(content: str):
    """Returns (frontmatter_lines, body) or None if malformed."""
    lines = strip_fence(content).split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None
    body = "\n".join(lines[end + 1:]).strip()
    return lines[1:end], body


def no_em_dash(text: str) -> str:
    """deepseek sometimes writes an em-dash unprompted; normalize it out of
    the written page mechanically rather than trust model phrasing."""
    return text.replace("\u2014", " - ")


def render_page(fm_lines: list, body: str, source_hash: str, fetched: str,
                 fingerprint: str) -> str:
    fm_lines = [no_em_dash(l) for l in fm_lines if not l.strip().startswith("kind:")]
    fm = ["---", "kind: summary"] + fm_lines + [
        f"source: {source_hash}", f"fetched: {fetched}", f"model: {MODEL}",
        f"prompt_fingerprint: {fingerprint}", "---",
    ]
    return "\n".join(fm) + "\n\n" + no_em_dash(body) + "\n"


def main() -> None:
    api_key = os.environ.get("LLM_WIKI_API_KEY")
    if not api_key:
        print("LLM_WIKI_API_KEY is not set", file=sys.stderr)
        sys.exit(1)

    articles = ([(t, "cooking") for t in COOKING]
                + [(t, "security") for t in SECURITY])
    domain_index = {"cooking": 0, "security": 0}
    jobs = []
    for title, domain in articles:
        slug = slugify(title)
        short_stub = domain_index[domain] % 3 == 0
        domain_index[domain] += 1
        jobs.append({"title": title, "domain": domain, "slug": slug,
                      "short_stub": short_stub})

    fingerprints = {stub: hashlib.sha256(build_instructions(stub).encode()).hexdigest()[:12]
                     for stub in (True, False)}

    def cache_path(job):
        return SUMMARY_CACHE / f"{job['slug']}-{fingerprints[job['short_stub']]}.json"

    planned = sum(1 for j in jobs if not cache_path(j).exists())
    print(f"planned paid calls: {planned}, estimated cost: "
          f"${planned * EST_COST_PER_CALL_USD:.3f}")

    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    fetched_date = time.strftime("%Y-%m-%d")
    pages, this_run_spend, paid_calls, total_spend = [], 0.0, 0, 0.0

    for job in jobs:
        source_text = fetch_source(job["slug"], job["title"])
        if not source_text:
            print(f"SKIP (fetch failed): {job['title']}")
            continue

        limit = SHORT_CHARS if job["short_stub"] else LONG_CHARS
        fed_text = source_text[:limit]
        fingerprint = fingerprints[job["short_stub"]]
        instructions = build_instructions(job["short_stub"])
        prompt = instructions + "\n\n---\n\nSource document:\n\n" + fed_text

        if this_run_spend > SPEND_CAP_USD:
            print(f"STOPPING: spend cap ${SPEND_CAP_USD} exceeded")
            break

        try:
            data, was_paid = call_openrouter(api_key, job["slug"], fingerprint, prompt)
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            print(f"SKIP (API error): {job['title']}: {exc}")
            continue

        cost = float((data.get("usage") or {}).get("cost") or 0.0)
        total_spend += cost
        if was_paid:
            this_run_spend += cost
            paid_calls += 1

        try:
            content = data["choices"][0]["message"]["content"]
            parsed = split_page(content)
            if parsed is None:
                raise ValueError("no frontmatter delimiters in response")
            if not parsed[1].strip():
                raise ValueError("empty body")
        except (KeyError, IndexError, ValueError) as exc:
            print(f"SKIP (bad response): {job['title']}: {exc}")
            continue

        fm_lines, body = parsed
        source_hash = hashlib.sha256(fed_text.encode()).hexdigest()[:12]
        page_text = render_page(fm_lines, body, source_hash, fetched_date, fingerprint)
        (PAGES_DIR / f"{job['slug']}.md").write_text(page_text, encoding="utf-8")

        pages.append({
            "slug": job["slug"], "domain": job["domain"], "title": job["title"],
            "source_title": job["title"], "body_chars": len(body),
            "short_stub": job["short_stub"], "source_chars": len(fed_text),
        })

    pages.sort(key=lambda p: p["slug"])
    manifest = {"pages": pages, "spend_usd": round(total_spend, 6),
                "model": MODEL, "generated": fetched_date}
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"wrote {len(pages)} pages, this run: {paid_calls} paid calls, "
          f"${this_run_spend:.4f}; total recorded spend: ${total_spend:.4f}")


if __name__ == "__main__":
    main()
