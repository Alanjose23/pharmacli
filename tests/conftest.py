import os

import pytest

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
        # a callable reply sees the request, so a test can answer each prompt
        # differently and keep what was sent
        text = reply(body) if callable(reply) else reply
        pieces = [text[i:i + 3] for i in range(0, len(text), 3)]
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


# ── patient profiles: accounts, sessions and a patient database per test ────

PASSWORD = "correct horse battery"          # 21 characters; the floor is 12

# Labels for the patient features. Four drugs: gammavir has two labels, and
# deltaquin's generic name ends in another drug's, the way "losartan potassium
# and hydrochlorothiazide" does in the real corpus.
PATIENT_CORPUS = {
    "inserts": [
        {"doc_id": "alpha", "title": "ALPHAZINE", "generic": "ALPHAZINE HYDROCHLORIDE",
         "version": 2, "label_date": "2026-01-02", "ndcs": ["11111111101"]},
        {"doc_id": "beta", "title": "BETAMOL", "generic": "BETAMOL",
         "version": 5, "label_date": "2026-02-03", "ndcs": ["22222222201"]},
        {"doc_id": "gamma", "title": "GAMMAVIR", "generic": "GAMMAVIR SODIUM",
         "version": 1, "label_date": "2026-03-04", "ndcs": ["33333333301"]},
        {"doc_id": "gamma-er", "title": "GAMMAVIR ER",
         "generic": "GAMMAVIR SODIUM EXTENDED-RELEASE",
         "version": 4, "label_date": "2026-04-05", "ndcs": ["33333333401"]},
        {"doc_id": "delta", "title": "DELTAQUIN PLUS", "generic": "DELTAQUIN AND BETAMOL",
         "version": 3, "label_date": "2026-05-06", "ndcs": ["44444444401"]},
    ],
    "business": [],
}


@pytest.fixture(autouse=True, scope="session")
def _patient_database_stays_out_of_the_repository(tmp_path_factory):
    # The API opens PHARMACLI_PATIENT_DB whenever a request needs it. Without
    # this, a suite run from the repository root would read and write the
    # developer's own patients.db. Session-wide so the module-scoped API
    # fixtures are covered too; a test that wants its own file sets it again.
    keep = {k: os.environ.get(k) for k in ("PHARMACLI_PATIENT_DB", "PHARMACLI_SCRYPT_N")}
    os.environ["PHARMACLI_PATIENT_DB"] = str(tmp_path_factory.mktemp("patients") / "suite.db")
    os.environ["PHARMACLI_SCRYPT_N"] = "16"         # the real cost is 2^17: 0.3s a hash
    yield
    for key, value in keep.items():
        os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)


@pytest.fixture(autouse=True)
def _no_recall_lookups(monkeypatch):
    # a patient report asks openFDA for recent recalls; no test may reach the network
    from pharmacli import review

    monkeypatch.setattr(review, "_recalls", lambda day: ())


@pytest.fixture
def clinic(tmp_path, monkeypatch):
    # the API over an empty patient database holding one pharmacist, "rph"
    pytest.importorskip("fastapi")
    from contextlib import closing

    from fastapi.testclient import TestClient

    db = str(tmp_path / "patients.db")
    monkeypatch.setenv("PHARMACLI_JWT_SECRET", "unit-test-secret")
    monkeypatch.setenv("PHARMACLI_MODEL", str(tmp_path / "absent.joblib"))
    monkeypatch.setenv("PHARMACLI_DB", str(tmp_path / "absent.db"))
    monkeypatch.setenv("PHARMACLI_PATIENT_DB", db)
    from pharmacli import patients, serve

    with closing(patients.connect(db)) as conn:
        patients.add_account(conn, "rph", PASSWORD, "pharmacist", 16)
    with TestClient(serve.app) as c:
        c.db = db
        # the test labels, not whatever corpus.json the working directory holds
        serve.app.dependency_overrides[serve.get_corpus] = lambda: PATIENT_CORPUS
        # and no background pair checks against whatever model the machine runs:
        # the queue is driven directly, with the stand-in model, in test_review
        serve.S.pop("wake", None)
        yield c
        serve.app.dependency_overrides.clear()


def signin(client, username="rph", password=PASSWORD) -> dict:
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def patient_login(client, pid, username="pat") -> dict:
    # a patient account for an existing record, past its one-time password
    from contextlib import closing

    from pharmacli import patients

    with closing(patients.connect(client.db)) as conn:
        _, once = patients.set_login(conn, pid, username, 16)
    first = signin(client, username, once)
    r = client.post("/auth/password", json={"current": once, "new": PASSWORD}, headers=first)
    assert r.status_code == 200, r.text
    return signin(client, username)


# ── label PDFs and class terms for the patient features ─────────────────────

CLASSES = {
    "aliases": {"Testase Inhibitor [EPC]": ["TSI"]},
    "drugs": {
        "alphazine": {"pharm_class": ["Testase Inhibitor [EPC]"]},
        "betamol": {"pharm_class": ["Cytochrome P450 3A4 Inducers [MoA]"]},
        "gammavir": {"pharm_class": ["Nucleoside Analog Antiviral [EPC]"]},
        "deltaquin": {"pharm_class": [], "terms": [
            {"term": "quinoline", "source": "DELTAQUIN PLUS label, p.2"}]},
    },
}


def label(folder, doc_id, *, boxed=(), dose=(), contra=(), warn=(), inter=(), renal=(),
          hepatic=(), pregnancy=(), counsel=("Take it with food.",)):
    # a label in the current numbered format: Highlights and contents on page 1
    # (never quoted), the sections a check reads on page 2, the rest on page 3
    front = ["HIGHLIGHTS OF PRESCRIBING INFORMATION",
             "This summary names betamol, gammavir and deltaquin and is never quoted.",
             "FULL PRESCRIBING INFORMATION: CONTENTS", "1 INDICATIONS AND USAGE",
             "2 DOSAGE AND ADMINISTRATION", "4 CONTRAINDICATIONS",
             "5 WARNINGS AND PRECAUTIONS", "7 DRUG INTERACTIONS",
             "8 USE IN SPECIFIC POPULATIONS", "17 PATIENT COUNSELING INFORMATION"]
    body = [*(["WARNING: SERIOUS RISKS", *boxed] if boxed else []),
            "1 INDICATIONS AND USAGE", "Treats testitis in adults.",
            "2 DOSAGE AND ADMINISTRATION", "2.1 Usual Dose", "Take 10 mg once daily.", *dose,
            "4 CONTRAINDICATIONS", "Known hypersensitivity to the product.", *contra,
            "5 WARNINGS AND PRECAUTIONS", "5.1 Liver Injury", "Liver injury has been reported.",
            *warn, "7 DRUG INTERACTIONS", "7.1 Other Drugs", "Food does not change absorption.",
            *inter]
    back = ["8 USE IN SPECIFIC POPULATIONS",
            *(["8.1 Pregnancy", *pregnancy] if pregnancy else []),
            *(["8.6 Renal Impairment", *renal] if renal else []),
            *(["8.7 Hepatic Impairment", *hepatic] if hepatic else []),
            "17 PATIENT COUNSELING INFORMATION", *counsel]
    # two more pages, so a heading on the contents page and again in the text is
    # on 2 of 5 pages: rag.parse drops a line on more than half as a running header
    makepdf(folder / f"{doc_id}.pdf", front, body, back,
            ["11 DESCRIPTION", f"A white tablet ({doc_id})."],
            ["16 HOW SUPPLIED/STORAGE AND HANDLING", f"Bottles of 30 ({doc_id})."])


@pytest.fixture
def labels(tmp_path):
    label(tmp_path, "alpha",
          contra=["Do not use alphazine with betamol."],
          inter=["Reduce the alphazine dose by half when it is taken with gammavir."],
          warn=["Monitor liver enzymes when alphazine is used with deltaquin."],
          renal=["Reduce the dose to 5 mg when creatinine clearance is below 30 mL/min."],
          hepatic=["Avoid alphazine in severe hepatic impairment."],
          pregnancy=["Alphazine may cause fetal harm."])
    label(tmp_path, "beta", pregnancy=["No human data are available."])
    label(tmp_path, "gamma", warn=["Monitor liver enzymes when gammavir is used with deltaquin."])
    label(tmp_path, "gamma-er")
    label(tmp_path, "delta", dose=["Deltaquin and betamol tablets are taken once daily."])
    from pharmacli import review

    return review.Labels(PATIENT_CORPUS, CLASSES, str(tmp_path))
