# Week 9: answers from package inserts and reimbursement documents, with the
# page each sentence came from. Parse and chunk, index into Chroma, retrieve,
# refuse what the documents cannot support, and stream a cited answer.

import bisect
import collections
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from pharmacli import docs
from pharmacli.config import Config, load
from pharmacli.enrich import pad

log = logging.getLogger(__name__)


# ── parse and chunk ──────────────────────────────────────────────────────────

# ~800 characters sits inside all-MiniLM-L6-v2's 256-token window. Both
# strategies use the same budget, so the eval compares where chunks break,
# not how big they are.
SIZE = 800
OVERLAP = 150

Line = tuple[int, str]          # (page number, text)


@dataclass(frozen=True)
class Chunk:
    text: str
    section: str
    page_start: int
    page_end: int


def parse(path: str) -> list[Line]:
    import pymupdf

    with pymupdf.open(path) as doc:
        pages = [[ln.strip() for b in page.get_text("blocks", sort=True) if b[6] == 0
                  for ln in b[4].splitlines() if ln.strip()] for page in doc]
    # A line on more than half the pages is a running header or footer: the
    # CFR's print stamp, eMedNY's "NYRx". DailyMed inserts have none.
    seen = collections.Counter(ln for p in pages for ln in set(p))
    repeated = {ln for ln, n in seen.items() if len(pages) > 2 and n > len(pages) / 2}
    blank = sum(1 for p in pages if not p)
    if blank:
        # ponytail: no OCR. A scanned page has no text layer and is skipped;
        # add OCR when a corpus document actually needs it.
        log.warning("%s: %d of %d pages have no text layer", path, blank, len(pages))
    return [(n, ln) for n, p in enumerate(pages, 1) for ln in p if ln not in repeated]


_NUMBERED = re.compile(r"(\d{1,2}(?:\.\d{1,2})*)(\.?)\s+([A-Z].*)")
_SECTION = re.compile(r"(§\s*\d+[\w.-]*?)\.?(?:\s+([A-Z][^,;]*))?")


def heading(line: str) -> str | None:
    # Fitted to the real corpus: PLR inserts ("5 WARNINGS AND PRECAUTIONS",
    # "5.1 Lactic Acidosis"), eMedNY ("1.0 General Pharmacy Policy"), the NY
    # statute ("4. Appeals.") and the CFR ("§ 447.502"). An all-caps-line rule
    # was tried and dropped: it matched eMedNY form fields like "ZZZZZZZZ".
    s = line.strip()
    if len(s) > 100:
        return None
    if s.startswith(("WARNING:", "BOXED WARNING")):
        return s
    m = _NUMBERED.fullmatch(s)
    if m:
        num, dot, rest = m.groups()
        title = rest.split(". ")[0].rstrip(".")
        # "2 Tablets twice daily" is dosing text, not a heading: a bare number
        # only heads a section when the title is in capitals, as PLR sets it
        if "." not in num and not dot and not title.isupper():
            return None
        return f"{num} {title}"[:80]
    m = _SECTION.fullmatch(s)
    if m:
        num, rest = m.groups()
        return (f"{num} {rest.split('. ')[0].rstrip('.')}" if rest else num)[:80]
    return None


def _page_at(starts: list[tuple[int, int]], offset: int) -> int:
    return starts[bisect.bisect_right(starts, offset, key=lambda s: s[0]) - 1][1]


def chunk_fixed(lines: list[Line], size: int = SIZE, overlap: int = OVERLAP) -> list[Chunk]:
    text, starts = "", []
    for page, ln in lines:
        # a line ending in a hyphen continues on the next one. Keep the hyphen:
        # "metformin-associated" is a real compound, and dropping it corrupts
        # far more words than keeping it in a split one does
        sep = "" if not text or text.endswith("-") else " "
        starts.append((len(text) + len(sep), page))
        text += sep + ln
    if not text:
        return []
    return [Chunk(text[i:i + size], "", _page_at(starts, i),
                  _page_at(starts, min(i + size, len(text)) - 1))
            for i in range(0, max(len(text) - overlap, 1), size - overlap)]


def sections(lines: list[Line]) -> list[tuple[str, list[Line]]]:
    # (heading, the lines under it); "" heads whatever precedes the first heading.
    # A heading can appear twice -- once in the table of contents with no body
    out: list[tuple[str, list[Line]]] = [("", [])]
    for page, ln in lines:
        h = heading(ln)
        if h:
            out.append((h, []))
        else:
            out[-1][1].append((page, ln))
    return out


def chunk_section(lines: list[Line], size: int = SIZE, overlap: int = OVERLAP) -> list[Chunk]:
    out = []
    for title, body in sections(lines):
        # a heading with no body is a table-of-contents line: chunk_fixed of
        # nothing is nothing, so it makes no chunk
        budget = max(200, size - len(title) - 3)
        for c in chunk_fixed(body, budget, min(overlap, budget // 4)):
            out.append(Chunk(f"{title} · {c.text}" if title else c.text,
                             title, c.page_start, c.page_end))
    return out


STRATEGIES = {"fixed": chunk_fixed, "section": chunk_section}


def collection(chunking: str) -> str:
    if chunking not in STRATEGIES:
        raise ValueError(f"chunking must be one of {sorted(STRATEGIES)}, got {chunking!r}")
    return f"chunks_{chunking}"


# ── Chroma and Ollama over HTTP ──────────────────────────────────────────────

@dataclass(frozen=True)
class Hit:
    id: str
    text: str
    meta: dict
    score: float            # cosine similarity, 1.0 is identical


class Chroma:
    # Chroma's v2 REST API through httpx: five calls, no chromadb package
    # (whose client drags in grpcio and OpenTelemetry), and async for free.
    # Verified against chromadb/chroma:1.5.9.

    def __init__(self, url: str, http: httpx.AsyncClient):
        self.url = url.rstrip("/")
        self.base = (f"{self.url}/api/v2/tenants/default_tenant"
                     "/databases/default_database/collections")
        self.http = http
        self._ids: dict[str, str] = {}

    async def ensure(self, name: str) -> str:
        if name not in self._ids:
            r = await self.http.post(self.base, json={
                "name": name, "metadata": {"hnsw:space": "cosine"}, "get_or_create": True})
            r.raise_for_status()
            self._ids[name] = r.json()["id"]
        return self._ids[name]

    async def upsert(self, name: str, ids: list[str], embeddings: list[list[float]],
                     documents: list[str], metadatas: list[dict]) -> None:
        r = await self.http.post(f"{self.base}/{await self.ensure(name)}/upsert", json={
            "ids": ids, "embeddings": embeddings, "documents": documents,
            "metadatas": metadatas})
        r.raise_for_status()

    async def query(self, name: str, embedding: list[float], k: int,
                    where: dict | None = None) -> list[Hit]:
        body: dict = {"query_embeddings": [embedding], "n_results": k,
                      "include": ["documents", "metadatas", "distances"]}
        if where:
            body["where"] = where
        r = await self.http.post(f"{self.base}/{await self.ensure(name)}/query", json=body)
        r.raise_for_status()
        d = r.json()
        # strict: Chroma returns four parallel arrays. If they ever came back
        # ragged, zip would pair a passage with another passage's metadata and
        # the answer would carry a citation to the wrong page -- silently
        return [Hit(i, t, m, 1.0 - dist) for i, t, m, dist in
                zip(d["ids"][0], d["documents"][0], d["metadatas"][0],
                    d["distances"][0], strict=True)]

    async def delete(self, name: str, where: dict) -> None:
        r = await self.http.post(f"{self.base}/{await self.ensure(name)}/delete",
                                 json={"where": where})
        r.raise_for_status()

    async def meta(self, name: str) -> dict | None:
        r = await self.http.get(f"{self.base}/{name}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("metadata") or {}

    async def set_meta(self, name: str, metadata: dict) -> None:
        # PUT replaces the whole metadata map, so the distance setting rides along
        r = await self.http.put(f"{self.base}/{await self.ensure(name)}",
                                json={"new_metadata": {"hnsw:space": "cosine", **metadata}})
        r.raise_for_status()


async def embed(http: httpx.AsyncClient, url: str, model: str,
                texts: list[str]) -> list[list[float]]:
    r = await http.post(f"{url}/api/embed", json={"model": model, "input": texts})
    r.raise_for_status()
    return r.json()["embeddings"]


async def chat_stream(http: httpx.AsyncClient, url: str, model: str, messages: list[dict]):
    import json

    # num_predict caps a runaway answer; 400 tokens is well past five sentences
    async with http.stream("POST", f"{url}/api/chat", json={
            "model": model, "messages": messages, "stream": True,
            "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 400}}) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if line.strip():
                yield json.loads(line)


# ── one session: config, one HTTP client, the index, the corpus ─────────────

@dataclass
class Rag:
    cfg: Config
    http: httpx.AsyncClient
    chroma: Chroma
    corpus: dict


@asynccontextmanager
async def session(cfg: Config | None = None, corpus_path: str = docs.CORPUS):
    cfg = cfg or load()
    corpus = docs.load_corpus(corpus_path)
    # generation on a CPU can sit silent a long time before its first token
    # when requests queue, so reads get a long leash; connects still fail fast
    async with httpx.AsyncClient(timeout=httpx.Timeout(cfg.timeout, read=600.0)) as http:
        yield Rag(cfg, http, Chroma(cfg.chroma_url, http), corpus)


# ── ingest ───────────────────────────────────────────────────────────────────

BATCH = 64


def _metadata(entry: dict, kind: str, c: Chunk) -> dict:
    # Chroma metadata cannot hold None, so absent fields are "" and 0
    return {"doc_type": kind, "doc_id": entry["doc_id"], "title": entry["title"],
            "section": c.section, "page_start": c.page_start, "page_end": c.page_end,
            "label_version": entry.get("version", 0), "label_date": entry.get("label_date", ""),
            "jurisdiction": entry.get("jurisdiction", "")}


async def ingest(rag: Rag, docs_dir: str = docs.DOCS_DIR, only: set[str] | None = None) -> dict:
    sha = docs.digest(rag.corpus)
    if only is None:
        marks = [await rag.chroma.meta(collection(s)) for s in STRATEGIES]
        if all(m and m.get("corpus_sha") == sha for m in marks):
            return {"skipped": True}

    report: dict = {"skipped": False, "docs": 0, "missing": [],
                    "chunks": dict.fromkeys(STRATEGIES, 0)}
    entries = ([(e, "insert") for e in rag.corpus["inserts"]]
               + [(b, "business") for b in rag.corpus["business"]])
    for entry, kind in entries:
        if only is not None and entry["doc_id"] not in only:
            continue
        path = docs.pdf_path(entry["doc_id"], docs_dir)
        if not path.exists():
            report["missing"].append(entry["doc_id"])
            continue
        lines = parse(str(path))
        for name, split in STRATEGIES.items():
            chunks = split(lines)
            # delete first: a refreshed label with fewer chunks must not keep
            # its old tail answering questions
            await rag.chroma.delete(collection(name), {"doc_id": entry["doc_id"]})
            for i in range(0, len(chunks), BATCH):
                batch = chunks[i:i + BATCH]
                texts = [f"{entry['title']} · {c.text}" for c in batch]
                vecs = await embed(rag.http, rag.cfg.ollama_url, rag.cfg.embed_model, texts)
                await rag.chroma.upsert(
                    collection(name), [f"{entry['doc_id']}:{i + j}" for j in range(len(batch))],
                    vecs, texts, [_metadata(entry, kind, c) for c in batch])
            report["chunks"][name] += len(chunks)
        report["docs"] += 1
        log.info("indexed %s", entry["doc_id"])

    # only a run with nothing missing may mark the corpus indexed, or the next
    # start would skip the documents that were missing this time
    if not report["missing"]:
        for name in STRATEGIES:
            await rag.chroma.set_meta(collection(name), {"corpus_sha": sha})
    return report


async def reindex(rag: Rag, changed: list[str], before: str,
                  docs_dir: str = docs.DOCS_DIR) -> dict:
    # re-index only what refresh changed, and only when the index was complete
    # for the corpus as it stood before the refresh; otherwise a failed earlier
    # ingest or a partial index would be stamped current. A full ingest skips in
    # seconds when nothing is stale.
    marks = [await rag.chroma.meta(collection(s)) for s in STRATEGIES]
    complete = all(m and m.get("corpus_sha") == before for m in marks)
    return await ingest(rag, docs_dir, only=set(changed) if changed and complete else None)


# ── answer ───────────────────────────────────────────────────────────────────

# Label facts only. These phrasings ask for a decision about one person, which
# a 3B model reading an insert must not make. Label phrasings stay allowed:
# "patients 65 years and older", "patients weighing less than 50 kg",
# "is metformin safe in pregnancy" all quote the label.
# ponytail: regex heuristic, paraphrases slip through; the prompt repeats the
# rule and the eval's refusal rows measure the gap.
_PATIENT = re.compile(
    # re.X ignores the unescaped whitespace, so the alternatives can sit on
    # their own lines. Never put a \n escape in here: re.X keeps escaped
    # whitespace, and the pattern would then demand a literal newline.
    r"\bmy\s+(patient|client|customer|mom|mother|dad|father|wife|husband|son|daughter|child|kid|baby)\b"
    r"  | \bshould\s+(i|we|he|she|they)\s+(give|take|start|stop|switch|use|double|skip|crush)\b"
    r"  | \b(can|may)\s+(i|we)\s+(give|double|switch|substitute)\b"
    r"  | \bis\s+(it|this|that)\s+safe\b"
    r"  | \b(a|an|my|this)\s+\d{1,3}[\s-]*(y/?o|years?[\s-]old|kg|lbs?|pounds?)\b"
    r"  | \b(crcl|egfr|scr)\s*(of|is|=|:)\s*\d",
    re.I | re.X)

PATIENT_MSG = ("I quote what the documents say; I can't make a decision about a specific "
               "patient. Ask what the label says instead, e.g. \"what does the {drug} label "
               "say about renal impairment\".")
NOT_IN_CORPUS = ("The documents I have don't cover that, so I won't guess. I answer from "
                 "package inserts and federal and New York reimbursement documents.")
SENTINEL = "NOT_IN_CONTEXT"
SYSTEM = (
    "You answer a pharmacist's question using ONLY the numbered context blocks.\n"
    "Rules:\n"
    "- End every sentence with the citation of the block it comes from, like [1] or [2][3].\n"
    '- Example of the required format: "The first fact from the context [2]. '
    'A second fact from the context [1]."\n'
    "- Say what the documents say. Do not add facts, doses or numbers that are "
    "not in the context.\n"
    "- Never make a decision about a specific patient or tailor a dose to one.\n"
    f"- If the context does not answer the question, reply with exactly: {SENTINEL}\n"
    "- At most 5 sentences.")

CITE = re.compile(r"\[(\d+)\]")


def patient_specific(question: str) -> bool:
    return bool(_PATIENT.search(question))


def resolve(question: str, corpus: dict) -> tuple[list[str], str | None]:
    from pharmacli.ask import NDC

    m = NDC.search(question)
    ndc = None
    if m:
        raw = m.group().replace(" ", "")
        # 16714-963-01 is 5-3-2: pad each part, never zfill the joined digits
        ndc = pad(raw) if raw.count("-") == 2 else re.sub(r"\D", "", raw).zfill(11)
    ids = [e["doc_id"] for e in corpus["inserts"] if ndc and ndc in e["ndcs"]]
    words = set(re.findall(r"[a-z0-9]+", question.lower()))
    for e in corpus["inserts"]:
        # the first word of the generic or brand name, whole: "metformin" picks
        # the metformin label, not SITAGLIPTIN AND METFORMIN, whose first word
        # differs. ponytail: first-word match, a synonym table if pharmacists
        # ask by class ("statins")
        names = {e["generic"].split()[0].lower(), e["title"].split()[0].lower()}
        if names & words and e["doc_id"] not in ids:
            ids.append(e["doc_id"])
    return ids, (f"NDC {ndc}" if ndc and not ids else None)


def cite(meta: dict) -> str:
    pages = f"p.{meta['page_start']}"
    if meta["page_end"] != meta["page_start"]:
        pages += f"-{meta['page_end']}"
    parts = [meta["title"], meta.get("section") or "", pages,
             f"label {meta['label_date']}" if meta.get("label_date") else ""]
    return " · ".join(p for p in parts if p)


def source(n: int, hit: Hit, text: bool = False) -> dict:
    m = hit.meta
    out = {"n": n, "doc_id": m["doc_id"], "doc_type": m["doc_type"], "title": m["title"],
           "section": m.get("section", ""), "page_start": m["page_start"],
           "page_end": m["page_end"], "label_date": m.get("label_date", ""),
           "score": round(hit.score, 4), "cite": cite(m)}
    return out | ({"text": hit.text} if text else {})


def _blocks(hits: list[Hit]) -> str:
    return "\n\n".join(f"[{n}] {cite(h.meta)}\n{h.text}" for n, h in enumerate(hits, 1))


def build_prompt(question: str, hits: list[Hit]) -> list[dict]:
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Context:\n{_blocks(hits)}\n\nQuestion: {question}\n\n"
                                        "Answer from the context only, and end every sentence "
                                        "with its [n] citation."}]


def check_citations(answer: str, n: int) -> tuple[list[int], bool]:
    valid = sorted({int(x) for x in CITE.findall(answer)} & set(range(1, n + 1)))
    return valid, bool(valid)


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


async def _search(rag: Rag, vector: list[float], k: int, chunking: str,
                  doc_ids: list[str], timings: dict) -> list[Hit]:
    t = time.perf_counter()
    where = {"doc_id": {"$in": doc_ids}} if doc_ids else None
    hits = await rag.chroma.query(collection(chunking), vector, k, where)
    timings["retrieve"] = _ms(t)
    return hits


async def _embed_question(rag: Rag, question: str, timings: dict) -> list[float]:
    t = time.perf_counter()
    [vector] = await embed(rag.http, rag.cfg.ollama_url, rag.cfg.embed_model, [question])
    timings["embed"] = _ms(t)
    return vector


async def retrieve(rag: Rag, question: str, k: int, chunking: str) -> list[Hit]:
    timings: dict = {}
    doc_ids, _ = resolve(question, rag.corpus)
    return await _search(rag, await _embed_question(rag, question, timings),
                         k, chunking, doc_ids, timings)


async def stream_answer(rag: Rag, question: str, k: int | None = None,
                        chunking: str | None = None):
    k, chunking = k or rag.cfg.k, chunking or rag.cfg.chunking
    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    tokens = {"prompt": 0, "completion": 0}
    retrieved: list[Hit] = []

    def done() -> dict:
        return {"type": "done", "tokens": tokens,
                "retrieved": [source(n, h, text=True) for n, h in enumerate(retrieved, 1)],
                "timings_ms": {**timings, "total": _ms(t0)}}

    def refusal(reason: str, message: str) -> dict:
        return {"type": "refusal", "reason": reason, "message": message}

    def error(code: str, exc: Exception) -> dict:
        return {"type": "error", "code": code, "detail": f"{type(exc).__name__}: {exc}"}

    doc_ids, unknown = resolve(question, rag.corpus)
    if patient_specific(question):
        drug = next((e["generic"].split()[0].lower() for e in rag.corpus["inserts"]
                     if e["doc_id"] in doc_ids), "drug")
        yield refusal("patient_specific", PATIENT_MSG.format(drug=drug))
        yield done()
        return
    if unknown:
        yield refusal("not_in_corpus", f"{unknown} has no label in the corpus, "
                                       "so I have nothing to quote for it.")
        yield done()
        return
    try:
        vector = await _embed_question(rag, question, timings)
    except httpx.HTTPError as exc:
        yield error("llm_unavailable", exc)
        yield done()
        return
    try:
        retrieved = await _search(rag, vector, k, chunking, doc_ids, timings)
    except httpx.HTTPError as exc:
        yield error("index_unavailable", exc)
        yield done()
        return
    if not retrieved or retrieved[0].score < rag.cfg.relevance_floor:
        yield refusal("not_in_corpus", NOT_IN_CORPUS)
        yield done()
        return

    answer, pending, t = "", "", time.perf_counter()
    try:
        async for part in chat_stream(rag.http, rag.cfg.ollama_url, rag.cfg.llm,
                                      build_prompt(question, retrieved)):
            text = part.get("message", {}).get("content", "")
            if text:
                timings.setdefault("ttft", _ms(t))
                answer += text
                head = answer.lstrip()
                # hold tokens back while they could still spell the sentinel,
                # so a refusal never reaches the screen half-written
                if len(head) < len(SENTINEL) and SENTINEL.startswith(head):
                    pending += text
                    continue
                if head.startswith(SENTINEL):
                    continue
                yield {"type": "token", "text": pending + text}
                pending = ""
            if part.get("done"):
                tokens = {"prompt": part.get("prompt_eval_count", 0),
                          "completion": part.get("eval_count", 0)}
                timings["prefill"] = part.get("prompt_eval_duration", 0) / 1e6
                timings["decode"] = part.get("eval_duration", 0) / 1e6
                # time to first token not spent loading the model or reading the
                # prompt was spent waiting behind other requests
                timings["queue"] = round(max(0.0, timings.get("ttft", 0.0) - timings["prefill"]
                                             - part.get("load_duration", 0) / 1e6), 1)
    except httpx.HTTPError as exc:
        yield error("llm_unavailable", exc)
        yield done()
        return

    if answer.lstrip().startswith(SENTINEL):
        yield refusal("not_in_context", NOT_IN_CORPUS)
    else:
        if pending:                          # a short answer that only looked like the sentinel
            yield {"type": "token", "text": pending}
        cited, grounded = check_citations(answer, len(retrieved))
        yield {"type": "sources", "grounded": grounded,
               "items": [source(n, retrieved[n - 1]) for n in cited]}
    yield done()


async def collect(rag: Rag, question: str, **kw) -> dict:
    out: dict = {"answer": "", "sources": [], "retrieved": [], "refused": False,
                 "reason": None, "message": None, "grounded": None, "error": None,
                 "timings_ms": {}, "tokens": {}}
    async for ev in stream_answer(rag, question, **kw):
        kind = ev["type"]
        if kind == "token":
            out["answer"] += ev["text"]
        elif kind == "sources":
            out["sources"], out["grounded"] = ev["items"], ev["grounded"]
        elif kind == "refusal":
            out |= {"refused": True, "reason": ev["reason"], "message": ev["message"]}
        elif kind == "error":
            out["error"] = f"{ev['code']}: {ev['detail']}"
        elif kind == "done":
            out |= {"retrieved": ev["retrieved"], "timings_ms": ev["timings_ms"],
                    "tokens": ev["tokens"]}
    return out


async def ask_once(question: str, **kw) -> dict:
    try:
        async with session() as r:
            return await collect(r, question, **kw)
    except (OSError, ValueError) as exc:          # no corpus.json, or an unreadable one
        return {"error": f"no corpus: {exc}"}


def format_lines(result: dict) -> list[str]:
    if result["refused"]:
        return [result["message"]]
    if not result["grounded"]:
        return ["the answer cited none of the passages it was given, so it is not shown.",
                "try naming the drug, or ask about one section of its label."]
    return ([result["answer"].strip(), ""]
            + [f"[{s['n']}] {s['cite']}" for s in result["sources"]])


# ── MAC appeal ───────────────────────────────────────────────────────────────

APPEAL_QUERY = "maximum allowable cost appeal pharmacy reimbursement below acquisition cost"
APPEAL_SYSTEM = (
    "You draft the argument paragraph of a pharmacy's maximum allowable cost (MAC) appeal.\n"
    "Use ONLY the facts table and the numbered context blocks.\n"
    "- Cite every sentence that relies on a rule with its block, like [1].\n"
    "- Refer to the facts by name. Do not invent numbers, dates or deadlines.\n"
    "- One formal paragraph, at most 6 sentences.")

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def appeal_facts(db: str, ndc: str) -> dict | str:
    from pathlib import Path

    from pharmacli.ask import _drug
    from pharmacli.desk import breakeven

    if not Path(db).exists():
        return f"no NADAC panel at {db}"
    ndc = ndc.strip().zfill(11)
    got = _drug(db, ndc)
    if not got:
        return f"{ndc} is not in the NADAC panel"
    d, cost, (_, monthly), _ = got
    if cost is None:
        return f"no acquisition cost recorded for {ndc}"
    nadac = float(d["unit_price"])
    gap = cost - nadac
    if gap <= 0:
        return f"{ndc} is not underwater: your cost {cost:.5f} is at or below NADAC {nadac:.5f}"
    fee = load().dispensing_fee
    # every number here comes from the panel and the shelf, never the model
    facts = {"NDC": ndc, "drug": d["description"], "NADAC file": d["as_of_date"],
             "NADAC per unit": f"{nadac:.5f}", "your cost per unit": f"{cost:.5f}",
             "gap per unit": f"{gap:.5f}", "pricing unit": d["pricing_unit"],
             "dispensing fee": f"{fee:.2f}",
             "break-even": breakeven(cost, nadac, fee, d["pricing_unit"])}
    if monthly:
        facts |= {"monthly units": f"{monthly:g}", "monthly loss": f"{gap * monthly:.2f}"}
    return facts


def numbers_not_in(text: str, *sources: str) -> list[str]:
    known = {n.replace(",", "") for s in sources for n in _NUM.findall(s)}
    return sorted({n for n in _NUM.findall(CITE.sub("", text)) if n.replace(",", "") not in known})


def render_appeal(facts: dict, argument: str, cited: list[tuple[int, Hit]],
                  flagged: list[str]) -> str:
    out = [f"# MAC appeal: {facts['drug']} ({facts['NDC']})", "",
           "| fact | value |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in facts.items()]
    out += ["", "## Argument", "", argument.strip() or "(the model returned nothing)",
            "", "## Sources", ""]
    out += [f"[{n}] {cite(h.meta)}" for n, h in cited] or ["none cited -- do not send this draft"]
    if flagged:
        out += ["", f"**Check before sending:** {', '.join(flagged)} appear in neither "
                    "the facts nor the sources."]
    out += ["", "_Your PBM contract's appeal window is not in these documents. "
                "Check it before filing._"]
    return "\n".join(out) + "\n"


async def draft_appeal(rag: Rag, facts: dict) -> dict:
    [vector] = await embed(rag.http, rag.cfg.ollama_url, rag.cfg.embed_model, [APPEAL_QUERY])
    hits = await rag.chroma.query(collection(rag.cfg.chunking), vector, rag.cfg.k,
                                  {"doc_type": "business"})
    table = "\n".join(f"- {k}: {v}" for k, v in facts.items())
    messages = [{"role": "system", "content": APPEAL_SYSTEM},
                {"role": "user", "content": f"Facts:\n{table}\n\nContext:\n{_blocks(hits)}"
                                            "\n\nWrite the argument paragraph."}]
    argument = "".join([p.get("message", {}).get("content", "") async for p in
                        chat_stream(rag.http, rag.cfg.ollama_url, rag.cfg.llm, messages)])
    cited, grounded = check_citations(argument, len(hits))
    flagged = numbers_not_in(argument, table, *(h.text for h in hits))
    return {"markdown": render_appeal(facts, argument, [(n, hits[n - 1]) for n in cited], flagged),
            "grounded": grounded, "flagged": flagged}
