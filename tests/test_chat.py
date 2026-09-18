import json
import socketserver
import threading
import urllib.request

from conftest import row
from pharmacli import chat
from pharmacli.models.medicine import Medicine
from pharmacli.store import connect, insert

CSV = b"ndc,cost,on_hand,monthly\n00000000001,1.50,100,200\n"


def server(db):
    chat.Handler.db = db
    chat.Handler.upload = None
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), chat.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def post(url, body=b""):
    with urllib.request.urlopen(urllib.request.Request(url, data=body, method="POST")) as r:
        return json.load(r)


def get(url):
    with urllib.request.urlopen(url) as r:
        return json.load(r)


def tuploadthenaskusesthefile(tmp_path):
    db = str(tmp_path / "c.db")
    conn = connect(db)
    insert(conn, [Medicine.arow(row("00000000001", "1.00000", "2026-01-07"), 1)])
    conn.close()

    srv, base = server(db)
    try:
        up = post(base + "/upload", CSV)
        assert up["lines"] == 1 and up["skipped"] == 1        # the header row
        assert any(s["q"] == "what am I losing money on" for s in up["suggest"])

        answered = get(base + "/ask?q=what+am+I+losing+money+on")
        assert "not saved" in answered["text"]
        assert "1 positions cost more than NADAC" in answered["text"]

        saved = post(base + "/save")
        assert saved["saved"] == 1
        assert chat.Handler.upload is None                    # saved, so the slot clears

        answered = get(base + "/ask?q=what+am+I+losing+money+on")
        assert "not saved" not in answered["text"]            # now from the database
        assert "1 positions cost more than NADAC" in answered["text"]
    finally:
        srv.shutdown()


def tuploadwithnousablerowsreportswhy(tmp_path):
    srv, base = server(str(tmp_path / "c.db"))
    try:
        up = post(base + "/upload", b"not,a,csv\n")
        assert up["lines"] == 0 and up["suggest"] == []
        assert "not digits" in up["reasons"][0]
        assert chat.Handler.upload is None
    finally:
        srv.shutdown()


def tclearforgetstheupload(tmp_path):
    srv, base = server(str(tmp_path / "c.db"))
    try:
        post(base + "/upload", CSV)
        assert chat.Handler.upload is not None
        post(base + "/clear")
        assert chat.Handler.upload is None
    finally:
        srv.shutdown()
