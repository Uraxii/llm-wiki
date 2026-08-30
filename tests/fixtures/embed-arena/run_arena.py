#!/usr/bin/env python3
"""Embedding model arena.

Embeds the fixture corpus and query set with local (ollama) and hosted
(OpenRouter) embedding models, scores retrieval, and prints one markdown
table. Standard library only.
"""
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OLLAMA_URL = "http://127.0.0.1:11434/api/embed"
OPENROUTER_URL = "https://openrouter.ai/api/v1/embeddings"
TOP_K = 10
RELEVANCE_CUTOFF = 0.5
MAX_CHARS = 2000
HOSTED_BATCH = 16
LOCAL_BATCH = 8
SPEND_LIMIT_USD = 1.50
CACHE_DIR = Path("/tmp/arena-embed/cache")

LOCAL_MODELS = [
    "embeddinggemma:latest",
    "nomic-embed-text:latest",
    "bge-m3:latest",
    "qwen3-embedding:0.6b",
]
HOSTED_MODELS = [
    "voyageai/voyage-4-lite",
    "openai/text-embedding-3-small",
    "google/gemini-embedding-2",
    "qwen/qwen3-embedding-8b",
    "nvidia/nemotron-3-embed-1b:free",
    "baai/bge-m3",
    "liquid/lfm-2.5-embedding-350m:free",
]

# model_name -> (doc_prefix, query_prefix). Everything else gets ("", "").
PREFIXES = {
    "nomic-embed-text:latest": ("search_document: ", "search_query: "),
    "embeddinggemma:latest": (
        "title: none | text: ",
        "task: search result | query: ",
    ),
}

TRUNCATION_NOTES = {
    "liquid/lfm-2.5-embedding-350m:free": "512 token context, inputs truncate",
}


def parse_page(path):
    """Hand-rolled frontmatter parser: fences, key: value, and '  - x' lists."""
    text = path.read_text()
    parts = text.split("---\n")
    frontmatter = parts[1] if len(parts) > 1 else ""
    body = "---\n".join(parts[2:]).lstrip("\n") if len(parts) > 2 else ""
    title = ""
    identifiers = []
    in_identifiers = False
    for line in frontmatter.splitlines():
        if in_identifiers and line.startswith("  - "):
            identifiers.append(line[4:].strip())
            continue
        in_identifiers = False
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if key == "title":
                title = value
            elif key == "identifiers":
                in_identifiers = True
    return title, identifiers, body


def page_text(title, identifiers, body):
    text = title + "\n" + ", ".join(identifiers) + "\n" + body
    return text[:MAX_CHARS]


def load_fixture(fixture_dir):
    pages_dir = fixture_dir / "pages"
    manifest_path = fixture_dir / "manifest.json"
    queries_path = fixture_dir / "queries.json"
    if not pages_dir.is_dir() or not manifest_path.exists() or not queries_path.exists():
        sys.exit(f"fixture not found under {fixture_dir} (need pages/, manifest.json, queries.json)")
    manifest = json.loads(manifest_path.read_text())
    queries = json.loads(queries_path.read_text())
    pages = []
    for entry in manifest["pages"]:
        slug = entry["slug"]
        title, identifiers, body = parse_page(pages_dir / f"{slug}.md")
        pages.append({
            "slug": slug,
            "text": page_text(title, identifiers, body),
            "short_stub": bool(entry.get("short_stub")),
        })
    return pages, queries


def http_post_json(url, payload, headers=None, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def call_local(model, texts):
    result = http_post_json(OLLAMA_URL, {"model": model, "input": texts})
    return result["embeddings"], 0.0, 0


def call_hosted(model, texts, api_key):
    headers = {"Authorization": f"Bearer {api_key}"}
    result = http_post_json(
        OPENROUTER_URL, {"model": model, "input": texts}, headers=headers
    )
    vectors = [item["embedding"] for item in result["data"]]
    usage = result.get("usage", {})
    return vectors, float(usage.get("cost", 0.0)), int(usage.get("prompt_tokens", 0))


def embed_in_batches(texts, batch_size, call_batch):
    """Batch requests; on an HTTP error, drop to single items and retry once."""
    vectors, total_cost, total_tokens, requests = [], 0.0, 0, 0
    i = 0
    while i < len(texts):
        batch = texts[i:i + batch_size]
        try:
            vecs, cost, tokens = call_batch(batch)
            requests += 1
        except urllib.error.URLError:
            vecs, cost, tokens = [], 0.0, 0
            for one in batch:
                v, c, t = call_batch([one])
                requests += 1
                vecs.extend(v)
                cost += c
                tokens += t
        vectors.extend(vecs)
        total_cost += cost
        total_tokens += tokens
        i += batch_size
    return vectors, total_cost, total_tokens, requests


def cache_path(model_name):
    safe = model_name.replace("/", "_").replace(":", "_")
    return CACHE_DIR / f"{safe}.json"


def load_cache(model_name):
    path = cache_path(model_name)
    return json.loads(path.read_text()) if path.exists() else None


def save_cache(model_name, cache):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path(model_name).write_text(json.dumps(cache))


def keys_for(pages, queries):
    return [f"page:{p['slug']}" for p in pages] + [
        f"query:{i}" for i in range(len(queries))
    ]


def cache_covers(model_name, keys):
    cache = load_cache(model_name)
    return cache is not None and all(k in cache["vectors"] for k in keys)


def run_model(candidate, pages, queries, api_key):
    """Return (result_dict, new_cost_usd, new_paid_requests)."""
    name, kind = candidate["name"], candidate["kind"]
    doc_prefix, query_prefix = PREFIXES.get(name, ("", ""))
    keys = keys_for(pages, queries)
    if cache_covers(name, keys):
        return load_cache(name), 0.0, 0
    texts = [doc_prefix + p["text"] for p in pages]
    texts += [query_prefix + q["q"] for q in queries]
    batch_size = LOCAL_BATCH if kind == "local" else HOSTED_BATCH
    fetch = (
        (lambda b: call_local(name, b))
        if kind == "local"
        else (lambda b: call_hosted(name, b, api_key))
    )
    start = time.time()
    vectors, cost, tokens, requests = embed_in_batches(texts, batch_size, fetch)
    wall = time.time() - start
    result = {
        "vectors": dict(zip(keys, vectors)),
        "usage": {"prompt_tokens": tokens, "cost_usd": cost},
        "wall_seconds": wall,
        "dims": len(vectors[0]) if vectors else 0,
    }
    save_cache(name, result)
    return result, cost, requests


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def compute_metrics(vectors, pages, queries):
    slugs = [p["slug"] for p in pages]
    short_slugs = {p["slug"] for p in pages if p["short_stub"]}
    recall5, recall10, rr = [], [], []
    under_cutoff = 0
    short_ranks, long_ranks = [], []
    for i, q in enumerate(queries):
        qvec = vectors[f"query:{i}"]
        scored = sorted(
            ((cosine(qvec, vectors[f"page:{s}"]), s) for s in slugs),
            key=lambda pair: pair[0],
            reverse=True,
        )
        ranked = [s for _, s in scored]
        relevant = set(q["relevant"])
        if not relevant:
            continue
        recall5.append(sum(s in relevant for s in ranked[:5]) / len(relevant))
        recall10.append(sum(s in relevant for s in ranked[:10]) / len(relevant))
        rank = next((idx + 1 for idx, s in enumerate(ranked) if s in relevant), None)
        rr.append(1.0 / rank if rank else 0.0)
        under_cutoff += sum(1 for score, _ in scored[:TOP_K] if score < RELEVANCE_CUTOFF)
        for idx, s in enumerate(ranked):
            if s in relevant:
                (short_ranks if s in short_slugs else long_ranks).append(idx + 1)
    return {
        "recall5": statistics.mean(recall5) if recall5 else 0.0,
        "recall10": statistics.mean(recall10) if recall10 else 0.0,
        "mrr": statistics.mean(rr) if rr else 0.0,
        "under_cutoff": under_cutoff,
        "mean_rank_short": statistics.mean(short_ranks) if short_ranks else None,
        "mean_rank_long": statistics.mean(long_ranks) if long_ranks else None,
    }


def print_spend_estimate(hosted_candidates, pages, queries):
    pending = [c for c in hosted_candidates if not cache_covers(c["name"], keys_for(pages, queries))]
    if not pending:
        return
    num_texts = len(pages) + len(queries)
    requests = sum(math.ceil(num_texts / HOSTED_BATCH) for _ in pending)
    chars = sum(len(p["text"]) for p in pages) + sum(len(q["q"]) for q in queries)
    est_tokens = (chars // 4) * len(pending)
    est_cost = est_tokens * 0.00000002  # rough placeholder, real cost is measured
    print(f"planned hosted requests: {requests}, rough estimated cost: ${est_cost:.4f}")


def fmt(value, digits=3):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def build_row(candidate, result, metrics, num_pages):
    # Per the ticket the denominator is pages, not pages plus queries. The one-time
    # query embeddings are folded in, so these figures slightly overstate per-page cost.
    name, kind = candidate["name"], candidate["kind"]
    cost_usd = result["usage"]["cost_usd"]
    cost_per_1k = "-" if kind == "local" else fmt((cost_usd / num_pages) * 1000, 4)
    notes = []
    if name in PREFIXES:
        notes.append("prefix")
    if name in TRUNCATION_NOTES:
        notes.append(TRUNCATION_NOTES[name])
    if kind == "hosted":
        notes.append("wall time network-bound")
    return {
        "name": name,
        "kind": kind,
        "recall5": metrics["recall5"],
        "recall10": metrics["recall10"],
        "mrr": metrics["mrr"],
        "dims": result["dims"],
        "bytes_per_page": result["dims"] * 4,
        "cost_per_1k": cost_per_1k,
        "wall_per_1k": (result["wall_seconds"] / num_pages) * 1000,
        "under_cutoff": metrics["under_cutoff"],
        "mean_rank_short": metrics["mean_rank_short"],
        "mean_rank_long": metrics["mean_rank_long"],
        "notes": "; ".join(notes) if notes else "-",
    }


def print_table(rows):
    headers = [
        "model", "kind", "recall@5", "recall@10", "mrr", "dims", "bytes/page",
        "cost/1k pages", "wall s/1k", "under cutoff", "mean rank short",
        "mean rank long", "notes",
    ]
    print("| " + " | ".join(headers) + " |")
    print("|" + "|".join(["---"] * len(headers)) + "|")
    for r in rows:
        cells = [
            r["name"], r["kind"], fmt(r["recall5"]), fmt(r["recall10"]),
            fmt(r["mrr"]), str(r["dims"]), str(r["bytes_per_page"]),
            r["cost_per_1k"], fmt(r["wall_per_1k"]), str(r["under_cutoff"]),
            fmt(r["mean_rank_short"]), fmt(r["mean_rank_long"]), r["notes"],
        ]
        print("| " + " | ".join(cells) + " |")


def run_all(candidates, pages, queries, api_key):
    rows, errors = [], []
    total_spend, total_paid_requests = 0.0, 0
    num_pages = len(pages)
    for candidate in candidates:
        try:
            result, new_cost, new_requests = run_model(candidate, pages, queries, api_key)
        except Exception as exc:
            errors.append((candidate["name"], str(exc)))
            continue
        total_spend += new_cost
        total_paid_requests += new_requests
        if total_spend > SPEND_LIMIT_USD:
            print(f"spend limit exceeded: ${total_spend:.4f} > ${SPEND_LIMIT_USD:.2f}, stopping")
            break
        metrics = compute_metrics(result["vectors"], pages, queries)
        rows.append(build_row(candidate, result, metrics, num_pages))
    return rows, errors, total_spend, total_paid_requests


def main():
    fixture_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
    pages, queries = load_fixture(fixture_dir)
    api_key = os.environ.get("LLM_WIKI_API_KEY", "").strip()

    candidates = [{"name": m, "kind": "local"} for m in LOCAL_MODELS]
    if api_key:
        hosted = [{"name": m, "kind": "hosted"} for m in HOSTED_MODELS]
        print_spend_estimate(hosted, pages, queries)
        candidates += hosted
    else:
        print("LLM_WIKI_API_KEY not set: hosted models skipped, running local only.")

    rows, errors, total_spend, total_paid_requests = run_all(candidates, pages, queries, api_key)
    rows.sort(key=lambda r: r["recall5"], reverse=True)
    print_table(rows)

    print()
    print("Notes:")
    print(f"  prefixed models: {', '.join(PREFIXES.keys())}")
    print(f"  truncation warnings: {TRUNCATION_NOTES}")
    print(f"  total measured spend this run: ${total_spend:.4f}")
    print(f"  total paid requests this run: {total_paid_requests}")
    for name, err in errors:
        print(f"  FAILED {name}: {err}")


if __name__ == "__main__":
    main()
