from pharmacli.models.rows import COLUMNS, RawRow


def row(ndc: str, price: str, asof: str, rate: str = "G", generic: str = "") -> RawRow:
    return dict(  # type: ignore[return-value]
        zip(COLUMNS,
            [f"DRUG {ndc}", ndc, price, "01/01/2026", "EA", "C/I", "N", "1",
             rate, generic, "01/01/2026" if generic else "", asof], strict=True))


# ── week 9: PDFs, Chroma and Ollama, none of them real ───────────────────────

def makepdf(path, *pages):
    import pymupdf

    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        for i, text in enumerate(lines):
            page.insert_text((72, 72 + 18 * i), text)
    doc.save(str(path))


RAG_CORPUS = {
    "inserts": [
        {"doc_id": "met", "title": "METFORMIN HYDROCHLORIDE", "generic": "METFORMIN HYDROCHLORIDE",
         "version": 3, "label_date": "2026-01-02", "ndcs": ["11111111101"]},
        {"doc_id": "sitamet", "title": "ZITUVIMET",
         "generic": "SITAGLIPTIN AND METFORMIN HYDROCHLORIDE",
         "version": 6, "label_date": "2026-08-12", "ndcs": ["70771185906"]},
    ],
    "business": [
        {"doc_id": "ny-phl-280-a", "title": "NY Public Health Law §280-a", "jurisdiction": "NY",
         "url": "https://example.invalid/280-a.pdf", "sha256": "abc"},
    ],
}


def hit(n, score=0.8, doc="met", doc_type="insert", title="METFORMIN HYDROCHLORIDE",
        section="2.2 Renal Impairment", page=4, text=None):
    from pharmacli.rag import Hit

    return Hit(f"{doc}:{n}", text or f"{title} · passage {n}",
               {"doc_id": doc, "doc_type": doc_type, "title": title, "section": section,
                "page_start": page, "page_end": page, "label_version": 3,
                "label_date": "2026-01-02" if doc_type == "insert" else "",
                "jurisdiction": "" if doc_type == "insert" else "NY"}, score)


class FakeChroma:
    def __init__(self, hits=(), meta=None):
        self.hits, self.metadata, self.calls = list(hits), dict(meta or {}), []

    async def upsert(self, name, ids, embeddings, documents, metadatas):
        self.calls.append(("upsert", name, ids, documents, metadatas))

    async def query(self, name, embedding, k, where=None):
        self.calls.append(("query", name, k, where))
        return self.hits[:k]

    async def delete(self, name, where):
        self.calls.append(("delete", name, where))

    async def meta(self, name):
        return self.metadata.get(name)

    async def set_meta(self, name, metadata):
        self.calls.append(("set_meta", name, metadata))
        self.metadata[name] = metadata


def ollama(reply="", embedding=None, down=False):
    import json

    import httpx

    def handler(request):
        if down:
            raise httpx.ConnectError("ollama is down", request=request)
        body = json.loads(request.content)
        if request.url.path == "/api/embed":
            vec = embedding or (lambda text: [1.0, 0.0])
            return httpx.Response(200, json={"embeddings": [vec(t) for t in body["input"]]})
        pieces = [reply[i:i + 3] for i in range(0, len(reply), 3)]
        lines = [{"message": {"content": p}, "done": False} for p in pieces]
        lines.append({"message": {"content": ""}, "done": True, "prompt_eval_count": 120,
                      "eval_count": len(pieces), "prompt_eval_duration": 40_000_000,
                      "eval_duration": 90_000_000, "load_duration": 0})
        return httpx.Response(200, content="\n".join(map(json.dumps, lines)).encode())
    return handler


def userag(fn, chroma, corpus=RAG_CORPUS, **kw):
    # `await fn(rag)` against the fakes. reply/embedding/down shape the fake
    # Ollama; any other keyword overrides a config field
    import asyncio
    from dataclasses import replace

    import httpx

    from pharmacli import rag
    from pharmacli.config import load

    fake = {k: kw.pop(k) for k in ("reply", "embedding", "down") if k in kw}

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(ollama(**fake))) as http:
            # pinned so a developer's own .env (a different k, floor, or
            # chunking) cannot change what these tests assert; explicit kw
            # still wins, so a test can opt into a different value on purpose
            pinned = {"k": 5, "chunking": "section", "relevance_floor": 0.35}
            cfg = replace(load(), ollama_url="http://ollama", **(pinned | kw))
            return await fn(rag.Rag(cfg, http, chroma, corpus))
    return asyncio.run(go())
