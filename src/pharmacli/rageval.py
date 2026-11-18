# Week 9 evaluation. Does the documents lane find the right page, say what the
# page says, and refuse what it should? Scores a JSONL answer key and puts two
# runs side by side.

import json
import math
import re

from pharmacli import ask, rag, review

# sentence-to-source similarity at which a sentence counts as supported
FAITHFUL = 0.5


def load_rows(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def hit_rank(retrieved: list[dict], gold: list[dict]) -> int | None:
    # a chunk hits when it comes from a gold document and its pages cover a
    # gold page -- page, not chunk id, so one answer key scores both strategies
    for rank, r in enumerate(retrieved, 1):
        if any(r["doc_id"] == g["doc_id"] and r["page_start"] <= g["page"] <= r["page_end"]
               for g in gold):
            return rank
    return None


def _norm(s: str) -> str:
    # "2,550 mg" and "2550  MG" are the same fact
    return re.sub(r"\s+", " ", s.lower().replace(",", "")).strip()


def fact_recall(answer: str, facts: list[str]) -> float | None:
    if not facts:
        return None
    return sum(_norm(f) in _norm(answer) for f in facts) / len(facts)


def _cos(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    # strict: two embeddings of different length are not comparable, and the
    # truncated cosine it would return looks like a real score
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb) if na and nb else 0.0


async def faithfulness(r: rag.Rag, answer: str, cited_texts: list[str]) -> float:
    sentences = [s for s in (rag.CITE.sub("", x).strip()
                             for x in re.split(r"(?<=[.!?])\s+", answer)) if len(s) > 3]
    if not sentences or not cited_texts:
        return 0.0
    vecs = await rag.embed(r.http, r.cfg.ollama_url, r.cfg.embed_model, sentences + cited_texts)
    said, sources = vecs[:len(sentences)], vecs[len(sentences):]
    return sum(max(_cos(s, c) for c in sources) >= FAITHFUL for s in said) / len(said)


def _stale(corpus: dict, row: dict) -> bool:
    if not row.get("label_version") or not row.get("gold"):
        return False
    versions = {e["doc_id"]: e["version"] for e in corpus["inserts"]}
    return versions.get(row["gold"][0]["doc_id"]) != row["label_version"]


async def run(r: rag.Rag, rows: list[dict], chunking: str, k: int,
              retrieval_only: bool) -> dict:
    out = []
    for row in rows:
        if row.get("gate") == "patient":
            # A question phrased the way a patient asks it. Patients do not
            # reach the chat: the patient view's gate is what must refuse these,
            # and it needs no model, so it is scored here and now.
            out.append({"id": row["id"], "expect": "refuse", "gate": "patient",
                        "refused": not review.general_label_question(row["q"], r.corpus)})
            continue
        res: dict = {"id": row["id"], "expect": row["expect_lane"], "lane": ask.lane(row["q"])}
        if _stale(r.corpus, row):
            out.append(res | {"stale": True})
            continue
        gold = row.get("gold") or []
        if res["lane"] == "documents" and row["expect_lane"] != "price":
            if retrieval_only:
                hits = await rag.retrieve(r, row["q"], k, chunking)
                found = [rag.source(n, h) for n, h in enumerate(hits, 1)]
                res |= {"rank": hit_rank(found, gold), "top": found[0]["score"] if found else 0.0}
            else:
                got = await rag.collect(r, row["q"], k=k, chunking=chunking)
                cited = [got["retrieved"][s["n"] - 1]["text"] for s in got["sources"]]
                res |= {"rank": hit_rank(got["retrieved"], gold),
                        "top": got["retrieved"][0]["score"] if got["retrieved"] else 0.0,
                        "refused": got["refused"], "grounded": bool(got["grounded"]),
                        "error": got["error"],
                        # kept so a wrong score can be audited against what the
                        # model actually said, not just its pass/fail number
                        "answer": got["answer"], "reason": got["reason"],
                        "fact_recall": fact_recall(got["answer"], row.get("facts") or []),
                        "faithfulness": (await faithfulness(r, got["answer"], cited)
                                         if got["answer"] else 0.0),
                        "total_ms": got["timings_ms"].get("total"), "tokens": got["tokens"]}
        out.append(res)
    return {"config": {"chunking": chunking, "k": k, "retrieval_only": retrieval_only,
                       "llm": None if retrieval_only else r.cfg.llm,
                       "floor": r.cfg.relevance_floor},
            "rows": out, "summary": summarize(out)}


def _pct(xs: list[float], q: float) -> float | None:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def summarize(rows: list[dict]) -> dict:
    asked = [r for r in rows if r.get("gate") == "patient"]
    # the chat's own numbers are over the chat's own rows, as before
    rows = [r for r in rows if r.get("gate") != "patient"]
    live = [r for r in rows if not r.get("stale")]
    ans = [r for r in live if r["expect"] == "documents"]
    ref = [r for r in live if r["expect"] == "refuse"]
    s: dict = {"rows": len(live), "stale": len(rows) - len(live),
               "routing": sum(r["lane"] == ("price" if r["expect"] == "price" else "documents")
                              for r in live),
               "answerable": len(ans),
               "hit": sum(r.get("rank") is not None for r in ans),
               "mrr": round(sum(1 / r["rank"] for r in ans if r.get("rank")) / len(ans), 4)
               if ans else 0.0}
    if any("refused" in r for r in live):
        times = [r["total_ms"] for r in live if r.get("total_ms")]
        recalls = [r["fact_recall"] for r in ans if r.get("fact_recall") is not None]
        s |= {"refuse_rows": len(ref),
              "refused_correctly": sum(bool(r.get("refused")) for r in ref),
              "false_refusals": sum(bool(r.get("refused")) for r in ans),
              "grounded": sum(bool(r.get("grounded")) for r in ans),
              "fact_recall": round(sum(recalls) / len(recalls), 4) if recalls else 0.0,
              "faithfulness": round(sum(r.get("faithfulness", 0.0) for r in ans) / len(ans), 4)
              if ans else 0.0,
              "p50_ms": _pct(times, 0.5), "p95_ms": _pct(times, 0.95),
              "errors": sum(bool(r.get("error")) for r in live)}
    if asked:
        s |= {"patient_rows": len(asked), "patient_refused": sum(r["refused"] for r in asked)}
    return s


# which count each x/n metric is out of
DENOMINATOR = {"routing": "rows", "hit": "answerable", "refused_correctly": "refuse_rows",
               "false_refusals": "answerable", "grounded": "answerable",
               "patient_refused": "patient_rows"}


def _ok(r: dict) -> bool:
    if "refused" not in r:                       # a retrieval-only run
        return r.get("rank") is not None
    recall = r.get("fact_recall")
    return (bool(r.get("grounded")) and not r.get("refused")
            and (recall is None or recall >= 1.0))


def compare(a: dict, b: dict) -> list[str]:
    def name(run: dict) -> str:
        return f"{run['config']['chunking']} k={run['config']['k']}"

    out = [f"{'metric':<20}{name(a):>16}{name(b):>16}"]
    for key, va in a["summary"].items():
        if key not in b["summary"] or key in set(DENOMINATOR.values()) | {"stale"}:
            continue
        vb = b["summary"][key]
        if key in DENOMINATOR:
            va = f"{va}/{a['summary'][DENOMINATOR[key]]}"
            vb = f"{vb}/{b['summary'][DENOMINATOR[key]]}"
        out.append(f"{key:<20}{str(va):>16}{str(vb):>16}")

    def oks(run: dict) -> dict:
        return {r["id"]: _ok(r) for r in run["rows"]
                if r["expect"] == "documents" and not r.get("stale")}

    before, after = oks(a), oks(b)
    both = before.keys() & after.keys()
    gained = sorted(i for i in both if after[i] and not before[i])
    lost = sorted(i for i in both if before[i] and not after[i])
    # tens of questions cannot resolve a one- or two-question swing: the flips
    # are printed so a small delta reads as the noise it is
    out += ["", f"flipped +{len(gained)} / -{len(lost)}"]
    if gained:
        out.append(f"  now right: {', '.join(gained)}")
    if lost:
        out.append(f"  now wrong: {', '.join(lost)}")
    return out


# ── the pair answer key ──────────────────────────────────────────────────────
# Each row names two drugs and the category their labels support, with the
# passage it was taken from. Three numbers come out, and the middle one is the
# one that matters most: a severe pair must never land in a milder category.

async def run_pairs(r: rag.Rag, rows: list[dict], labels: review.Labels) -> dict:
    known = review.drugs(labels.corpus)
    versions = {e["doc_id"]: e["version"] for e in labels.corpus["inserts"]}
    out = []
    for row in rows:
        a, b = known.get(row["a"]), known.get(row["b"])
        # a key row is only as good as the label versions it was read from
        if not a or not b or any(versions.get(d) != v
                                 for d, v in (row.get("label_versions") or {}).items()):
            out.append({"id": row["id"], "stale": True})
            continue
        got = await review.check(r, a, b, labels)
        out.append({"id": row["id"], "expect": row["expect"], "got": got["status"],
                    # kept so a wrong category can be read against what was quoted
                    "reason": got["reason"], "quotes": got["quotes"]})
    return {"config": {"llm": r.cfg.llm, "prompt": review.PROMPT}, "rows": out,
            "summary": pair_summary(out)}


def pair_summary(rows: list[dict]) -> dict:
    live = [r for r in rows if not r.get("stale")]
    rank = review.CATEGORIES.index
    severe = [r for r in live if r["expect"] in review.CATEGORIES[:2]]
    stated = [r for r in live if r["expect"] != review.CATEGORIES[3]]
    return {"pairs": len(live), "stale": len(rows) - len(live),
            "agree": sum(r["got"] == r["expect"] for r in live),
            # a "nothing stated" row agrees by construction: the same search wrote
            # the key. These two leave those rows out.
            "stated_pairs": len(stated),
            "stated_agree": sum(r["got"] == r["expect"] for r in stated),
            "severe_pairs": len(severe),
            # the target is none: manual review is not milder, a lower category is
            "severe_placed_milder": sum(r["got"] in review.CATEGORIES
                                        and rank(r["got"]) > rank(r["expect"]) for r in severe),
            # what the strict checks cost in coverage
            "manual_review": sum(r["got"] == review.MANUAL for r in live)}
