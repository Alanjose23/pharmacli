"""Week 10 load test: hit /ask/stream until latency degrades, then say why.

    python tests/loadtest.py --n 12 --concurrency 1,2,4 --out latency.md

The questions come from tests/rag_qa.jsonl, so this is the real corpus and the
real retrieval path. Every request already carries its own stage timings back
from the server (embed, retrieve, prefill, decode, queue), so finding the
bottleneck is aggregation, not profiling.
"""

import argparse
import asyncio
import json
import pathlib
import statistics
import time

import httpx

QA = pathlib.Path(__file__).with_name("rag_qa.jsonl")
STAGES = ["embed", "retrieve", "queue", "prefill", "decode"]
FIX = {
    "embed": "batch or cache question embeddings, or move the embedding model off the "
             "same Ollama process the generator is queued on",
    "retrieve": "Chroma is the cost: shrink the collection, lower k, or give the HNSW "
                "index more memory",
    "queue": "requests are waiting behind each other in Ollama: raise OLLAMA_NUM_PARALLEL, "
             "or run a second generator",
    "prefill": "reading the prompt is the cost, and it is ~350 tokens of passage per "
               "retrieved chunk: drop k, shorten SIZE, or put the model on a GPU. A "
               "repeated question costs ~0.2s here, so a cache in front of identical "
               "questions is real but narrow",
    "decode": "token generation is the cost: a smaller model, a lower num_predict, or a "
              "machine with a GPU",
}


def questions(n: int, skip: int = 0) -> list[str]:
    # Every level gets its OWN slice. Measured: replaying a question drops its
    # median prefill from 22,162 ms to 183 ms, because Ollama caches the prompt
    # prefix -- so a level that reuses the previous level's questions measures
    # that cache and reports a 3x latency improvement under load that is not
    # real. 50 questions covers --n 16 at three levels.
    rows = [json.loads(line) for line in QA.read_text().splitlines() if line.strip()]
    qs = [r["q"] for r in rows]
    return [qs[(skip + i) % len(qs)] for i in range(n)]


async def one(client: httpx.AsyncClient, url: str, token: str, q: str) -> dict:
    t0 = time.perf_counter()
    out: dict = {"q": q, "ttft_client": None, "refused": False}
    async with client.stream("POST", f"{url}/ask/stream", json={"question": q},
                             headers={"Authorization": f"Bearer {token}"}) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            if ev["type"] == "token" and out["ttft_client"] is None:
                out["ttft_client"] = round((time.perf_counter() - t0) * 1000, 1)
            elif ev["type"] in ("refusal", "error"):
                out["refused"] = True
            elif ev["type"] == "done":
                out |= ev["timings_ms"]
                out["completion_tokens"] = ev["tokens"].get("completion", 0)
    out["wall"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


async def level(url: str, token: str, qs: list[str], concurrency: int) -> dict:
    gate = asyncio.Semaphore(concurrency)

    async def guarded(client, q):
        async with gate:
            return await one(client, url, token, q)

    # read timeout well past a cold CPU generation; connect still fails fast
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=900.0)) as client:
        t0 = time.perf_counter()
        rows = await asyncio.gather(*(guarded(client, q) for q in qs))
        elapsed = time.perf_counter() - t0
    return {"concurrency": concurrency, "rows": rows, "elapsed_s": round(elapsed, 1),
            "rps": round(len(rows) / elapsed, 3)}


def med(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return round(statistics.median(vals), 1) if vals else None


def p95(rows: list[dict], key: str) -> float | None:
    vals = sorted(r[key] for r in rows if r.get(key) is not None)
    return vals[min(len(vals) - 1, int(0.95 * len(vals)))] if vals else None


def report(runs: list[dict], url: str) -> str:
    out = [f"# Latency report — {url}", "",
           f"_{time.strftime('%Y-%m-%d %H:%M')}, {len(runs[0]['rows'])} questions per level, "
           "streamed from `POST /ask/stream`. Each level asks a different slice of "
           "`tests/rag_qa.jsonl`: a repeated question hits Ollama's prompt cache and "
           "reports a prefill it never paid._", "",
           "| concurrency | n | p50 wall ms | p95 wall ms | p50 first token ms | req/s |",
           "|---|---|---|---|---|---|"]
    for r in runs:
        out.append(f"| {r['concurrency']} | {len(r['rows'])} | {med(r['rows'], 'wall')} | "
                   f"{p95(r['rows'], 'wall')} | {med(r['rows'], 'ttft_client')} | {r['rps']} |")

    out += ["", "## Where the time goes (median ms per stage)", "",
            "| concurrency | " + " | ".join(STAGES) + " | answered |", "|---" * (len(STAGES) + 2) + "|"]
    for r in runs:
        answered = sum(1 for x in r["rows"] if not x["refused"])
        out.append(f"| {r['concurrency']} | "
                   + " | ".join(str(med(r["rows"], s)) for s in STAGES)
                   + f" | {answered}/{len(r['rows'])} |")

    # the bottleneck is whichever stage owns the most milliseconds at the
    # heaviest level that still answered anything
    last = runs[-1]["rows"]
    ranked = sorted(((med(last, s) or 0.0, s) for s in STAGES), reverse=True)
    ms, stage = ranked[0]
    share = ms / (med(last, "wall") or ms) * 100
    out += ["", "## Bottleneck", "",
            f"**{stage}** — {ms:.0f} ms of a {med(last, 'wall'):.0f} ms median answer "
            f"({share:.0f}%) at concurrency {runs[-1]['concurrency']}.", "",
            f"Fix: {FIX[stage]}.", "",
            "Second: " + f"{ranked[1][1]} at {ranked[1][0]:.0f} ms.", ""]
    if len(runs) > 1:
        first, heavy = runs[0], runs[-1]
        out.append(f"Degradation: p50 went {med(first['rows'], 'wall'):.0f} ms → "
                   f"{med(heavy['rows'], 'wall'):.0f} ms from concurrency "
                   f"{first['concurrency']} to {heavy['concurrency']}, while throughput went "
                   f"{first['rps']} → {heavy['rps']} req/s.")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--n", type=int, default=12, help="requests per concurrency level")
    ap.add_argument("--concurrency", default="1,2,4", help="levels to sweep, comma separated")
    ap.add_argument("--token", default=None, help="bearer token; minted locally if omitted")
    ap.add_argument("--out", default="latency.md")
    args = ap.parse_args()

    # minted here needs the same PHARMACLI_JWT_SECRET the server runs with
    token = args.token or __import__("pharmacli.serve", fromlist=["issue"]).issue("loadtest")
    runs = []
    for i, c in enumerate([int(x) for x in args.concurrency.split(",")]):
        qs = questions(args.n, skip=i * args.n)
        print(f"concurrency {c}: {args.n} questions ...", flush=True)
        run = asyncio.run(level(args.url, token, qs, c))
        print(f"  p50 {med(run['rows'], 'wall')} ms, p95 {p95(run['rows'], 'wall')} ms, "
              f"{run['rps']} req/s", flush=True)
        runs.append(run)

    pathlib.Path(args.out).write_text(report(runs, args.url))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
