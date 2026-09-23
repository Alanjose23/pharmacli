# The chat page's endpoints. They used to be a hand-rolled http.server in
# chat.py; they are routes on the FastAPI app now, so these drive the app
# directly instead of starting a thread and a socket.
import pytest
from conftest import row

from pharmacli.models.medicine import Medicine
from pharmacli.store import connect, insert

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

CSV = b"ndc,cost,on_hand,monthly\n00000000001,1.50,100,200\n"


@pytest.fixture
def chat(tmp_path, monkeypatch):
    db = str(tmp_path / "c.db")
    conn = connect(db)
    insert(conn, [Medicine.arow(row("00000000001", "1.00000", "2026-01-07"), 1)])
    conn.close()

    monkeypatch.setenv("PHARMACLI_DB", db)
    monkeypatch.setenv("PHARMACLI_MODEL", str(tmp_path / "absent.joblib"))
    monkeypatch.setenv("PHARMACLI_JWT_SECRET", "unit-test-secret")
    from pharmacli import serve
    serve.UPLOAD.clear()
    with TestClient(serve.app) as c:
        c.headers["Authorization"] = f"Bearer {serve.issue('chat')}"
        yield c
    serve.UPLOAD.clear()


def tthepageisopenbutitsdataisnot(chat):
    # a browser navigating to a page cannot send a header, so the shell is open
    assert chat.get("/chat", headers={"Authorization": ""}).status_code == 200
    # everything it then asks for is not
    assert chat.get("/chat/prompts", headers={"Authorization": ""}).status_code == 401
    assert chat.get("/chat/ask?q=hello", headers={"Authorization": ""}).status_code == 401


def tuploadthenaskusesthefile(chat):
    up = chat.post("/chat/upload", content=CSV,
                   headers={"X-Filename": "shelf.csv"}).json()
    assert up["lines"] == 1 and up["skipped"] == 1            # the header row
    assert any(s["q"] == "what am I losing money on" for s in up["suggest"])

    answered = chat.get("/chat/ask", params={"q": "what am I losing money on"}).json()
    assert "not saved" in answered["text"]
    assert "1 positions cost more than NADAC" in answered["text"]

    saved = chat.post("/chat/save").json()
    assert saved["saved"] == 1

    from pharmacli import serve
    assert "held" not in serve.UPLOAD                         # saved, so the slot clears

    answered = chat.get("/chat/ask", params={"q": "what am I losing money on"}).json()
    assert "not saved" not in answered["text"]                # now from the database
    assert "1 positions cost more than NADAC" in answered["text"]


def tuploadwithnousablerowsreportswhy(chat):
    from pharmacli import serve

    up = chat.post("/chat/upload", content=b"not,a,csv\n").json()
    assert up["lines"] == 0 and up["suggest"] == []
    assert "not digits" in up["reasons"][0]
    assert "held" not in serve.UPLOAD


def tclearforgetstheupload(chat):
    from pharmacli import serve

    chat.post("/chat/upload", content=CSV)
    assert "held" in serve.UPLOAD
    chat.post("/chat/clear")
    assert "held" not in serve.UPLOAD


def tanoversizedfileisrefusedbeforeitisread(chat):
    from pharmacli import serve

    big = b"x" * (serve.MAX_UPLOAD + 1)
    up = chat.post("/chat/upload", content=big, headers={"X-Filename": "big.csv"}).json()
    assert "larger than" in up["error"]
    assert "held" not in serve.UPLOAD
