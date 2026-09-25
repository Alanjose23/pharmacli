# Week 10: the service itself -- auth, streaming, the request id, the counters.
# Chroma and Ollama are the fakes from conftest, so these run in CI with no
# containers; what is being tested is the API around the RAG, not the RAG.
import json
import time

import pytest
from conftest import RAG_CORPUS, FakeChroma, hit, ollama

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

ANSWER = "Metformin is contraindicated in severe renal impairment [1]. Monitor renal function [2]."
QUESTION = {"question": "what does the metformin label say about renal impairment"}


@pytest.fixture
def api(tmp_path, monkeypatch):
    from dataclasses import replace

    import httpx

    monkeypatch.setenv("PHARMACLI_JWT_SECRET", "unit-test-secret")
    # no model and no panel: the container's normal first state, and it keeps
    # the fixture off the 165MB database
    monkeypatch.setenv("PHARMACLI_MODEL", str(tmp_path / "absent.joblib"))
    monkeypatch.setenv("PHARMACLI_DB", str(tmp_path / "absent.db"))

    from pharmacli import rag, serve
    from pharmacli.config import load

    http = httpx.AsyncClient(transport=httpx.MockTransport(ollama(reply=ANSWER)))
    cfg = replace(load(), ollama_url="http://ollama", k=3, chunking="section",
                  relevance_floor=0.35)
    fake = rag.Rag(cfg, http, FakeChroma([hit(1), hit(2), hit(3)]), RAG_CORPUS)

    with TestClient(serve.app) as c:           # TestClient runs lifespan
        serve.app.dependency_overrides[serve.get_rag] = lambda: fake
        c.headers["Authorization"] = f"Bearer {serve.issue('test')}"
        yield c
        serve.app.dependency_overrides.clear()


def tanopentokenroundtrips(api):
    from pharmacli import serve

    assert serve.verify(serve.issue("alice"))["sub"] == "alice"


def tnotokenis401(api):
    r = api.post("/ask", json=QUESTION, headers={"Authorization": ""})
    assert r.status_code == 401
    # the error names the id it was logged under, so a report is findable
    assert r.json()["request_id"] == r.headers["x-request-id"]


def tatamperedsignatureisrejected(api):
    from pharmacli import serve

    head, payload, sig = serve.issue("mallory").split(".")
    forged = json.loads(serve._unb64(payload)) | {"sub": "admin"}
    bad = f"{head}.{serve._b64(json.dumps(forged).encode())}.{sig}"
    assert api.post("/ask", json=QUESTION,
                    headers={"Authorization": f"Bearer {bad}"}).status_code == 401


def talgnoneisrejected(api):
    # the classic JWT hole: a token that declares it is unsigned must not be
    # taken at its word
    from pharmacli import serve

    payload = serve._b64(json.dumps({"sub": "admin", "exp": time.time() + 60}).encode())
    none = f"{serve._b64(json.dumps({'alg': 'none'}).encode())}.{payload}."
    assert api.post("/ask", json=QUESTION,
                    headers={"Authorization": f"Bearer {none}"}).status_code == 401


def tanexpiredtokenisrejected(api):
    from pharmacli import serve

    r = api.post("/ask", json=QUESTION,
                 headers={"Authorization": f"Bearer {serve.issue('slow', ttl=-1)}"})
    assert r.status_code == 401 and "expired" in r.json()["detail"]


def taskanswerswithitssources(api):
    b = api.post("/ask", json=QUESTION).json()
    assert "contraindicated" in b["answer"]
    assert b["grounded"] and len(b["sources"]) == 2           # it cited [1] and [2]
    assert b["sources"][0]["cite"].startswith("METFORMIN")
    assert b["timings_ms"]["embed"] >= 0 and b["tokens"]["completion"] > 0


def tashortquestionisrejectedbyschema(api):
    assert api.post("/ask", json={"question": "hi"}).status_code == 422


def tstreamsendsseeventsintheorderitfindsthem(api):
    with api.stream("POST", "/ask/stream", json=QUESTION) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "token" and kinds[-1] == "done"
    assert "sources" in kinds
    assert "".join(e["text"] for e in events if e["type"] == "token") == ANSWER


def tpredictis503withoutamodelratherthanacrash(api):
    r = api.post("/predict", json={"ndc": "00000000001"})
    assert r.status_code == 503 and "FileNotFoundError" in r.json()["detail"]


def thealthisopenandsayswhatismissing(api):
    b = api.get("/health", headers={"Authorization": ""}).json()
    assert b["status"] == "ok" and b["model"] is None
    assert "absent.joblib" in b["model_error"]
    assert "documents" in b


def tmetricscountrequestsandnamethestages(api):
    api.post("/ask", json=QUESTION)
    b = api.get("/metrics", headers={"Authorization": ""}).json()
    assert b["requests"]["POST /ask"] >= 1
    assert b["latency_ms"]["POST /ask"]["p50"] >= 0
    # the stage block is what identifies the bottleneck in the latency report
    assert {"embed", "retrieve", "total"} <= set(b["stage_ms"])
    assert b["tokens"]["completion"] > 0


def tanincomingrequestidiskept(api):
    r = api.get("/health", headers={"X-Request-ID": "trace-me-42"})
    assert r.headers["x-request-id"] == "trace-me-42"


def tanonobjectheaderis401notacrash(api):
    # unsigned garbage used to reach .get() on a JSON list before the signature
    # check, and came back as a 500
    from pharmacli import serve

    junk = f"{serve._b64(b'[1]')}.{serve._b64(b'{}')}.x"
    assert api.post("/ask", json=QUESTION,
                    headers={"Authorization": f"Bearer {junk}"}).status_code == 401


def tachunkeduploadpastthecapisrefused(api, monkeypatch):
    # no content-length on a streamed body, so the cap must count as it reads
    from pharmacli import serve

    monkeypatch.setattr(serve, "MAX_UPLOAD", 10)
    b = api.post("/chat/upload", content=iter([b"00000000001,1.0\n"] * 3)).json()
    assert "larger than" in b["error"]


def tadocidcannotclimboutofthedocsfolder():
    from pharmacli import docs

    with pytest.raises(ValueError):
        docs.pdf_path("../../etc/passwd")
    assert docs.pdf_path("ny-phl-280-a").name == "ny-phl-280-a.pdf"
