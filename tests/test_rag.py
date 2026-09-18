import asyncio
import json

import httpx
import pytest

from conftest import RAG_CORPUS, FakeChroma, hit, makepdf, userag
from pharmacli import rag


# ── parse and chunk ──────────────────────────────────────────────────────────

def theadingsfromrealdocuments():
    # every line was copied out of a real corpus PDF
    cases = {
        "5 WARNINGS AND PRECAUTIONS": "5 WARNINGS AND PRECAUTIONS",
        "5.1 Lactic Acidosis": "5.1 Lactic Acidosis",
        "10 OVERDOSAGE": "10 OVERDOSAGE",
        "1.0 General Pharmacy Policy": "1.0 General Pharmacy Policy",
        "4. Appeals. A pharmacy benefit manager shall, with respect to": "4 Appeals",
        "§ 447.502": "§ 447.502",
        "§ 280-a. Pharmacy benefit managers.  1. Definitions. As used":
            "§ 280-a Pharmacy benefit managers",
        "WARNING: LACTIC ACIDOSIS": "WARNING: LACTIC ACIDOSIS",
        # body text that happens to start with a number or a section sign
        "2 Tablets twice daily": None,
        "2 tablets": None,
        "85 FR 87101, Dec. 31, 2020;": None,
        "§ 447.400, FFP will be available at the": None,
        "30 mL/min/1.73 m": None,
    }
    for line, want in cases.items():
        assert rag.heading(line) == want, line


def lines(*pages):
    return [(n, ln) for n, text in enumerate(pages, 1) for ln in text.split("\n")]


def tfixedwindowsrespectbudgetandoverlap():
    body = lines(" ".join(f"w{i:03d}" for i in range(200)),
                 " ".join(f"x{i:03d}" for i in range(200)))
    chunks = rag.chunk_fixed(body, size=300, overlap=50)
    assert all(len(c.text) <= 300 for c in chunks)
    for a, b in zip(chunks, chunks[1:]):
        assert a.text[-50:] == b.text[:50]              # consecutive windows overlap
    assert chunks[0].page_start == 1 and chunks[-1].page_end == 2
    joined = body[0][1] + " " + body[1][1]
    assert joined.endswith(chunks[-1].text)             # nothing dropped at the end
    assert any(c.page_start == 1 and c.page_end == 2 for c in chunks)


def tsectionchunksstayinsidetheirheading():
    body = lines("HIGHLIGHTS preamble\n5 WARNINGS AND PRECAUTIONS\n5.1 Lactic Acidosis\n"
                 + "lactic " * 150,
                 "5.2 Hypoglycemia\n" + "glucose " * 20)
    chunks = rag.chunk_section(body, size=300, overlap=50)
    # "5 WARNINGS AND PRECAUTIONS" has no body of its own, like a contents line
    assert {c.section for c in chunks} == {"", "5.1 Lactic Acidosis", "5.2 Hypoglycemia"}
    assert all(len(c.text) <= 300 for c in chunks)
    for c in chunks:
        if c.section == "5.1 Lactic Acidosis":
            assert c.text.startswith("5.1 Lactic Acidosis · ") and "glucose" not in c.text
        if c.section == "5.2 Hypoglycemia":
            assert "lactic" not in c.text and c.page_start == 2


def tparsedropsrunningheadersandkeepspages(tmp_path):
    pytest.importorskip("pymupdf")
    path = tmp_path / "doc.pdf"
    makepdf(path, *[["Policy Guidelines", f"5.{n} Section {n}",
                     f"text of page {n} metformin-", f"associated on page {n}"]
                    for n in (1, 2, 3)])
    got = rag.parse(str(path))
    assert "Policy Guidelines" not in {t for _, t in got}     # on every page: a running header
    assert (2, "5.2 Section 2") in got
    assert "metformin-associated on page 1" in rag.chunk_fixed(got)[0].text


def tunknownchunkingisrejected():
    assert rag.collection("section") == "chunks_section"
    with pytest.raises(ValueError):
        rag.collection("sentences")


# ── Chroma and Ollama over HTTP ──────────────────────────────────────────────

def tragsettingscomefromtheenvironment(monkeypatch):
    from pharmacli.config import load

    monkeypatch.setenv("PHARMACLI_K", "8")
    monkeypatch.setenv("PHARMACLI_CHUNKING", "fixed")
    monkeypatch.setenv("PHARMACLI_RELEVANCE_FLOOR", "not a number")
    monkeypatch.delenv("PHARMACLI_LLM", raising=False)
    cfg = load()
    assert cfg.k == 8 and cfg.chunking == "fixed"
    assert cfg.relevance_floor == 0.35               # a bad value falls back to the default
    assert cfg.llm == "qwen2.5:3b-instruct"


def tchromaspeaksthev2restapi():
    seen = []

    def handler(req):
        body = json.loads(req.content) if req.content else None
        seen.append((req.method, req.url.path, body))
        if req.url.path.endswith("/collections") and req.method == "POST":
            return httpx.Response(200, json={"id": "c1"})
        if req.url.path.endswith("/query"):
            return httpx.Response(200, json={"ids": [["d:0"]], "documents": [["text"]],
                                              "metadatas": [[{"doc_id": "d"}]],
                                              "distances": [[0.25]]})
        if req.url.path.endswith("/collections/missing"):
            return httpx.Response(404, json={"error": "NotFoundError"})
        return httpx.Response(200, json={})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            ch = rag.Chroma("http://chroma:8000/", http)
            await ch.upsert("chunks_section", ["d:0"], [[0.1]], ["text"], [{"doc_id": "d"}])
            hits = await ch.query("chunks_section", [0.1], 3, {"doc_id": {"$in": ["d"]}})
            await ch.set_meta("chunks_section", {"corpus_sha": "abc"})
            return hits, await ch.meta("missing")

    hits, missing = asyncio.run(go())
    assert hits == [rag.Hit("d:0", "text", {"doc_id": "d"}, 0.75)]   # cosine distance -> similarity
    assert missing is None
    base = "/api/v2/tenants/default_tenant/databases/default_database/collections"
    assert seen[0][:2] == ("POST", base)
    assert seen[0][2]["metadata"] == {"hnsw:space": "cosine"}
    assert sum(1 for m, p, _ in seen if (m, p) == ("POST", base)) == 1   # id cached
    query = next(b for _, p, b in seen if p == f"{base}/c1/query")
    assert query["n_results"] == 3 and query["where"] == {"doc_id": {"$in": ["d"]}}
    put = next(b for m, _, b in seen if m == "PUT")
    assert put == {"new_metadata": {"hnsw:space": "cosine", "corpus_sha": "abc"}}


def tollamaembedsandstreams():
    def handler(req):
        body = json.loads(req.content)
        if req.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[1.0, 0.0] for _ in body["input"]]})
        assert body["stream"] is True and body["options"]["temperature"] == 0
        parts = [{"message": {"content": "Hi"}, "done": False},
                 {"message": {"content": ""}, "done": True, "eval_count": 1}]
        return httpx.Response(200, content="\n".join(map(json.dumps, parts)).encode())

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            vecs = await rag.embed(http, "http://ollama:11434", "all-minilm:l6-v2", ["a", "b"])
            parts = [p async for p in rag.chat_stream(http, "http://ollama:11434", "qwen", [])]
            return vecs, parts

    vecs, parts = asyncio.run(go())
    assert vecs == [[1.0, 0.0], [1.0, 0.0]]
    assert [p["done"] for p in parts] == [False, True]


# ── ingest ───────────────────────────────────────────────────────────────────

def tingestindexesbothstrategiesandreportsmissing(tmp_path):
    pytest.importorskip("pymupdf")
    makepdf(tmp_path / "met.pdf", ["5.1 Lactic Acidosis", "metformin lactic text"])
    makepdf(tmp_path / "ny-phl-280-a.pdf",
            ["4. Appeals. A pharmacy benefit manager shall", "appeal text"])
    chroma = FakeChroma()
    report = userag(lambda r: rag.ingest(r, docs_dir=str(tmp_path)), chroma)

    assert report["missing"] == ["sitamet"] and report["docs"] == 2
    ups = [c for c in chroma.calls if c[0] == "upsert"]
    assert {c[1] for c in ups} == {"chunks_fixed", "chunks_section"}
    metas = [m for c in ups if c[1] == "chunks_section" for m in c[4]]
    assert {m["section"] for m in metas} == {"5.1 Lactic Acidosis", "4 Appeals"}
    doc = next(d for c in ups if c[1] == "chunks_section" for d in c[3] if "lactic text" in d)
    assert doc.startswith("METFORMIN HYDROCHLORIDE · 5.1 Lactic Acidosis · ")
    law = next(m for m in metas if m["doc_id"] == "ny-phl-280-a")
    assert (law["doc_type"], law["jurisdiction"], law["label_date"]) == ("business", "NY", "")
    for name in ("chunks_fixed", "chunks_section"):
        deleted = next(i for i, c in enumerate(chroma.calls)
                       if c[:3] == ("delete", name, {"doc_id": "met"}))
        upserted = next(i for i, c in enumerate(chroma.calls)
                        if c[:2] == ("upsert", name) and c[2][0].startswith("met:"))
        assert deleted < upserted                      # old chunks go before new ones land
    # a document was missing, so nothing may claim the corpus is indexed
    assert not any(c[0] == "set_meta" for c in chroma.calls)


def tacompleteingestrecordsthecorpushashandthenskips(tmp_path):
    pytest.importorskip("pymupdf")
    from pharmacli.docs import digest

    corpus = {"inserts": [RAG_CORPUS["inserts"][0]], "business": []}
    makepdf(tmp_path / "met.pdf", ["5.1 Lactic Acidosis", "text"])
    chroma = FakeChroma()
    userag(lambda r: rag.ingest(r, docs_dir=str(tmp_path)), chroma, corpus=corpus)
    assert chroma.metadata["chunks_section"] == {"corpus_sha": digest(corpus)}

    chroma.calls.clear()
    again = userag(lambda r: rag.ingest(r, docs_dir=str(tmp_path)), chroma, corpus=corpus)
    assert again == {"skipped": True} and chroma.calls == []


# ── refresh / reindex ─────────────────────────────────────────────────────────

def treindexonlychangedwhentheindexwascomplete(tmp_path):
    pytest.importorskip("pymupdf")
    from pharmacli.docs import digest

    makepdf(tmp_path / "met.pdf", ["5.1 Lactic Acidosis", "metformin lactic text"])
    makepdf(tmp_path / "ny-phl-280-a.pdf",
            ["4. Appeals. A pharmacy benefit manager shall", "appeal text"])
    before = digest(RAG_CORPUS)
    chroma = FakeChroma(meta={"chunks_fixed": {"corpus_sha": before},
                              "chunks_section": {"corpus_sha": before}})
    report = userag(lambda r: rag.reindex(r, ["met"], before, docs_dir=str(tmp_path)), chroma)

    ups = [c for c in chroma.calls if c[0] == "upsert"]
    ids = {i for c in ups for i in c[2]}
    assert ids and all(i.startswith("met:") for i in ids)
    assert not any(i.startswith("ny-phl-280-a:") for i in ids)
    assert report["docs"] == 1


def treindexfullwhentheindexwasstale(tmp_path):
    pytest.importorskip("pymupdf")

    makepdf(tmp_path / "met.pdf", ["5.1 Lactic Acidosis", "metformin lactic text"])
    makepdf(tmp_path / "ny-phl-280-a.pdf",
            ["4. Appeals. A pharmacy benefit manager shall", "appeal text"])
    chroma = FakeChroma(meta={"chunks_fixed": {"corpus_sha": "stale"},
                              "chunks_section": {"corpus_sha": "stale"}})
    report = userag(lambda r: rag.reindex(r, ["met"], "current", docs_dir=str(tmp_path)), chroma)

    ups = [c for c in chroma.calls if c[0] == "upsert"]
    ids = {i for c in ups for i in c[2]}
    assert any(i.startswith("met:") for i in ids)
    assert any(i.startswith("ny-phl-280-a:") for i in ids)
    assert report["docs"] == 2


def treindexskipswhennothingchangedandcurrent(tmp_path):
    from pharmacli.docs import digest

    sha = digest(RAG_CORPUS)
    chroma = FakeChroma(meta={"chunks_fixed": {"corpus_sha": sha},
                              "chunks_section": {"corpus_sha": sha}})
    report = userag(lambda r: rag.reindex(r, [], sha, docs_dir=str(tmp_path)), chroma)
    assert report == {"skipped": True}


# ── answer ───────────────────────────────────────────────────────────────────

def tpatientspecificquestionsarerefused():
    for q in ("what dose of lisinopril should I give",
              "is this safe with warfarin",
              "my 80 year old patient has a CrCl of 30, how much metformin",
              "can I give 2 tablets at once"):
        assert rag.patient_specific(q), q
    for q in ("what is the maximum daily dose of metformin",
              "what does the label say about patients 65 years and older",
              "what is the dose for patients weighing less than 50 kg",
              "is metformin safe in pregnancy",
              "what does the label say for eGFR below 30",
              "can this drug be crushed"):
        assert not rag.patient_specific(q), q


def tresolvefindsdrugsbyndcandname():
    assert rag.resolve("metformin max dose", RAG_CORPUS) == (["met"], None)
    assert rag.resolve("zituvimet renal dosing", RAG_CORPUS) == (["sitamet"], None)
    # 5-3-2 hyphenation pads per part; bare digits zero-fill
    assert rag.resolve("dose for 70771-1859-06", RAG_CORPUS) == (["sitamet"], None)
    assert rag.resolve("what about 11111111101", RAG_CORPUS) == (["met"], None)
    assert rag.resolve("storage for 99999999999", RAG_CORPUS) == ([], "NDC 99999999999")
    assert rag.resolve("how are MAC appeals handled", RAG_CORPUS) == ([], None)


def tcitationsoutsidetheretrievedsetaredropped():
    assert rag.check_citations("Stop below eGFR 30 [2]. See [9].", 3) == ([2], True)
    assert rag.check_citations("No citation at all.", 3) == ([], False)


def events(chroma, question, **kw):
    async def go(r):
        return [e async for e in rag.stream_answer(r, question)]
    return userag(go, chroma, **kw)


def tpatientrefusalneedsnonetwork():
    evs = events(FakeChroma(), "should I give my patient 2000 mg of metformin", down=True)
    assert [e["type"] for e in evs] == ["refusal", "done"]
    assert evs[0]["reason"] == "patient_specific" and "metformin label" in evs[0]["message"]


def tunknownndcisrefusednamingit():
    evs = events(FakeChroma([hit(1)]), "dosing for 99999999999")
    assert evs[0]["reason"] == "not_in_corpus" and "99999999999" in evs[0]["message"]


def tweakretrievalisrefused():
    evs = events(FakeChroma([hit(1, score=0.2)]), "what is the capital of France")
    assert [e["type"] for e in evs] == ["refusal", "done"]
    assert evs[0]["reason"] == "not_in_corpus"


def tgroundedanswerstreamswithsources():
    chroma = FakeChroma([hit(1, page=3), hit(2, page=4)])
    reply = "Do not use below eGFR 30 [2]. See [9]."
    evs = events(chroma, "what does the metformin label say about renal impairment", reply=reply)

    assert "".join(e["text"] for e in evs if e["type"] == "token") == reply
    sources = next(e for e in evs if e["type"] == "sources")
    assert sources["grounded"] and [s["n"] for s in sources["items"]] == [2]
    assert sources["items"][0]["cite"] == ("METFORMIN HYDROCHLORIDE · 2.2 Renal Impairment"
                                           " · p.4 · label 2026-01-02")
    done = evs[-1]
    assert done["type"] == "done" and done["tokens"]["prompt"] == 120
    assert {"embed", "retrieve", "ttft", "prefill", "decode", "queue", "total"} <= set(done["timings_ms"])
    assert [r["text"] for r in done["retrieved"]] == [hit(1).text, hit(2).text]
    # the question named metformin, so retrieval was filtered to its label
    assert chroma.calls[0] == ("query", "chunks_section", 5, {"doc_id": {"$in": ["met"]}})


def tsentinelisneverstreamed():
    evs = events(FakeChroma([hit(1)]), "what is the storage temperature", reply="NOT_IN_CONTEXT")
    assert not any(e["type"] == "token" for e in evs)
    assert evs[0] == {"type": "refusal", "reason": "not_in_context", "message": rag.NOT_IN_CORPUS}


def tollamadownisanerrorevent():
    evs = events(FakeChroma([hit(1)]), "what is the storage temperature", down=True)
    assert evs[0]["type"] == "error" and evs[0]["code"] == "llm_unavailable"
    assert evs[-1]["type"] == "done"


def tformatlineshidesungroundedanswers():
    base = {"refused": False, "message": None, "answer": "Take with meals [1].",
            "sources": [{"n": 1, "cite": "METFORMIN HYDROCHLORIDE · p.2"}]}
    assert rag.format_lines(base | {"grounded": True}) == [
        "Take with meals [1].", "", "[1] METFORMIN HYDROCHLORIDE · p.2"]
    assert "not shown" in rag.format_lines(base | {"grounded": False})[0]
    assert rag.format_lines(base | {"refused": True, "message": "no"}) == ["no"]


# ── MAC appeal ───────────────────────────────────────────────────────────────

def shelf(tmp_path):
    from conftest import row
    from pharmacli.models.medicine import Medicine
    from pharmacli.store import connect, insert, watch_cost

    db = str(tmp_path / "a.db")
    conn = connect(db)
    meds = [Medicine.arow(row("00000000001", "1.00000", "2026-01-07"), 1),
            Medicine.arow(row("00000000002", "5.00000", "2026-01-07"), 2)]
    insert(conn, [m for m in meds if isinstance(m, Medicine)])
    watch_cost(conn, [("00000000001", 1.50, 100.0, 200.0), ("00000000002", 1.00, 100.0, 200.0)])
    conn.close()
    return db


def tappealfactscomefromthepanel(tmp_path):
    db = shelf(tmp_path)
    facts = rag.appeal_facts(db, "00000000001")
    assert facts["gap per unit"] == "0.50000" and facts["monthly loss"] == "100.00"
    assert facts["NADAC file"] == "2026-01-07" and facts["break-even"]
    assert "not underwater" in rag.appeal_facts(db, "00000000002")
    assert "not in the NADAC panel" in rag.appeal_facts(db, "99999999999")
    assert "no NADAC panel" in rag.appeal_facts(str(tmp_path / "none.db"), "00000000001")


def tnumbersnotinthesourcesareflagged():
    text = "Cost 1.50000 exceeds NADAC 1.00000 [1] by 0.75 and must be answered within 7 days."
    assert rag.numbers_not_in(text, "your cost: 1.50000\nNADAC: 1.00000",
                              "respond within 7 business days") == ["0.75"]


def tappealdraftcitesreimbursementdocsonly():
    law = hit(1, doc="ny-phl-280-a", doc_type="business", title="NY Public Health Law §280-a",
              section="4 Appeals", page=2, text="appeal within 7 business days")
    chroma = FakeChroma([law])
    facts = {"NDC": "00000000001", "drug": "DRUG 00000000001", "gap per unit": "0.50000"}
    got = userag(lambda r: rag.draft_appeal(r, facts), chroma,
                 reply="The PBM must answer within 7 business days [1]. The gap is 9.99 [1].")
    assert chroma.calls[0][3] == {"doc_type": "business"}
    assert "| gap per unit | 0.50000 |" in got["markdown"]
    assert "[1] NY Public Health Law §280-a · 4 Appeals · p.2" in got["markdown"]
    assert got["flagged"] == ["9.99"] and got["grounded"]
