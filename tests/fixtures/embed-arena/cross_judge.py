#!/usr/bin/env python3
"""Cross-model judge for the embedding arena fixture.

Asks a non-deepseek, non-Claude LLM (default openai/gpt-4o-mini via
OpenRouter) to independently label which pages are relevant to each
hand-written query, then diffs its answer against queries.json.
Reports disagreements; never edits queries.json. Stdlib only.
"""
import hashlib
import json
import os
import sys
import urllib.request
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
JUDGE_MODEL = "openai/gpt-4o-mini"
FALLBACK_JUDGE_MODEL = "openai/gpt-4.1-mini"
CACHE_DIR = Path("/tmp/arena-embed/judge")
STUB_CHARS = 200
SPEND_LIMIT_USD = 0.50
COST_PER_CALL_ESTIMATE = 0.002  # ~4k tokens at gpt-4o-mini rates

SYSTEM_PROMPT = (
    "You judge search relevance for a wiki. Given a query and a list of "
    "candidate pages, return every page a person issuing that query would "
    "be glad to receive. Prefer precision: when unsure, leave a page out. "
    "Return an empty list if nothing fits. Use only slugs from the "
    "candidate list. Reply with JSON only, no prose, no markdown fence: "
    '{"relevant": ["slug", ...]}'
)

def parse_page(path):
    """Hand-rolled frontmatter parser: matches run_arena.py's approach."""
    text = path.read_text()
    parts = text.split("---\n")
    frontmatter = parts[1] if len(parts) > 1 else ""
    body = "---\n".join(parts[2:]).lstrip("\n") if len(parts) > 2 else ""
    title = ""
    for line in frontmatter.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            if key.strip() == "title":
                title = value.strip()
    return title, body

def load_pages(fixture_dir):
    pages_dir = fixture_dir / "pages"
    manifest = json.loads((fixture_dir / "manifest.json").read_text())
    pages = []
    for entry in manifest["pages"]:
        slug = entry["slug"]
        title, body = parse_page(pages_dir / f"{slug}.md")
        stub = body.replace("\n", " ").strip()[:STUB_CHARS]
        pages.append({"slug": slug, "title": title, "stub": stub})
    return pages

def cache_key(query_text, candidates_text):
    digest = hashlib.sha256((query_text + "\n" + candidates_text).encode())
    return digest.hexdigest()[:16]

def call_judge(model, query_text, candidates_text, api_key):
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Query: {query_text}\n\nCandidates:\n{candidates_text}",
            },
        ],
    }
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())

def load_cached(index, query_text, candidates_text):
    cache_path = CACHE_DIR / f"{index}.json"
    if not cache_path.exists():
        return None
    cached = json.loads(cache_path.read_text())
    if cached.get("key") != cache_key(query_text, candidates_text):
        return None
    return cached

def judge_query(index, query_text, candidates_text, model, api_key, spend):
    cached = load_cached(index, query_text, candidates_text)
    if cached is not None:
        return cached, 0.0, False
    response = call_judge(model, query_text, candidates_text, api_key)
    cost = float(response.get("usage", {}).get("cost", 0.0))
    spend[0] += cost
    if spend[0] > SPEND_LIMIT_USD:
        sys.exit(f"stopping: spend ${spend[0]:.4f} passed limit ${SPEND_LIMIT_USD}")
    content = response["choices"][0]["message"]["content"]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = cache_key(query_text, candidates_text)
    cached = {"key": key, "content": content, "cost": cost}
    (CACHE_DIR / f"{index}.json").write_text(json.dumps(cached))
    return cached, cost, True

def judge_with_fallback(index, query_text, candidates_text, model_box, api_key, spend):
    """Try model_box[0]; on a 404 (unknown model id) swap to the fallback."""
    try:
        return judge_query(
            index, query_text, candidates_text, model_box[0], api_key, spend
        )
    except HTTPError as exc:
        if exc.code != 404 or model_box[0] == FALLBACK_JUDGE_MODEL:
            raise
        model_box[0] = FALLBACK_JUDGE_MODEL
        return judge_query(
            index, query_text, candidates_text, model_box[0], api_key, spend
        )

def parse_judge_labels(raw_content, valid_slugs):
    """Strip a defensive markdown fence, then require strict JSON."""
    text = raw_content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text[: -3] if text.endswith("```") else text
    labels = json.loads(text.strip())["relevant"]
    return [s for s in labels if s in valid_slugs]

def jaccard(a, b):
    a, b = set(a), set(b)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)

def run(fixture_dir):
    api_key = os.environ["LLM_WIKI_API_KEY"]
    pages = load_pages(fixture_dir)
    slugs = {p["slug"] for p in pages}
    # Send every page, not a retrieval pre-filter: a pre-filter would use an
    # embedding model to narrow candidates before judging that same model's
    # ground truth, contaminating what this arena is trying to measure.
    candidates_text = "\n".join(
        f"{p['slug']} | {p['title']} | {p['stub']}" for p in pages
    )
    queries = json.loads((fixture_dir / "queries.json").read_text())

    uncached = sum(
        1
        for i, q in enumerate(queries)
        if load_cached(i, q["q"], candidates_text) is None
    )
    print(f"planned paid calls: {uncached} (~${uncached * COST_PER_CALL_ESTIMATE:.4f} est)")

    model_box = [JUDGE_MODEL]
    spend = [0.0]
    paid_calls = 0
    results = []
    for i, q in enumerate(queries):
        hand = q["relevant"]
        try:
            raw, cost, paid = judge_with_fallback(
                i, q["q"], candidates_text, model_box, api_key, spend
            )
            if paid:
                paid_calls += 1
            judge_labels = parse_judge_labels(raw["content"], slugs)
            error = None
        except (json.JSONDecodeError, KeyError, HTTPError, URLError) as exc:
            judge_labels = []
            error = f"{type(exc).__name__}: {exc}"
        hand_only = sorted(set(hand) - set(judge_labels))
        judge_only = sorted(set(judge_labels) - set(hand))
        entry = {
            "q": q["q"],
            "hand": hand,
            "judge": judge_labels,
            "hand_only": hand_only,
            "judge_only": judge_only,
            "jaccard": round(jaccard(hand, judge_labels), 4),
        }
        if error:
            entry["error"] = error
        results.append(entry)

    exact = sum(1 for r in results if not r["hand_only"] and not r["judge_only"])
    summary = {
        "queries": len(results),
        "exact_agreement": exact,
        "hand_only_total": sum(len(r["hand_only"]) for r in results),
        "judge_only_total": sum(len(r["judge_only"]) for r in results),
        "mean_jaccard": round(sum(r["jaccard"] for r in results) / len(results), 4)
        if results
        else 0.0,
    }
    report = {
        "judge_model": model_box[0],
        "generated": date.today().isoformat(),
        "spend_usd": round(spend[0], 4),
        "paid_calls": paid_calls,
        "summary": summary,
        "queries": results,
    }
    (fixture_dir / "judge_report.json").write_text(json.dumps(report, indent=2))

    print(f"\njudge model: {model_box[0]}  spend: ${spend[0]:.4f}  paid calls: {paid_calls}")
    print(f"summary: {summary}\n")
    for r in results:
        if r["hand_only"] or r["judge_only"]:
            print(f"DISAGREE: {r['q']}")
            print(f"  hand : {r['hand']}")
            print(f"  judge: {r['judge']}")
    return report

if __name__ == "__main__":
    fixture_arg = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent
    run(fixture_arg)
