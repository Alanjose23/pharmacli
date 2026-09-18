import hashlib

import httpx

from pharmacli import docs

PDF = b"%PDF-1.4 a real enough pdf"


def label(set_id, generic, packages, version="6"):
    return {"set_id": set_id, "version": version, "effective_time": "20260812",
            "openfda": {"generic_name": [generic], "brand_name": [generic.split()[0]],
                        "package_ndc": packages}}


def client(labels_by_product, pdf=PDF):
    def handler(req):
        if req.url.host == "api.fda.gov":
            search = req.url.params["search"]
            for key, item in labels_by_product.items():
                if f'"{key}"' in search:
                    return httpx.Response(200, json={"results": [item]})
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
        return httpx.Response(200, content=pdf)
    return httpx.Client(transport=httpx.MockTransport(handler))


def tlabelforndcconfirmsthepackage(monkeypatch):
    monkeypatch.setattr(docs, "PAUSE", 0)
    # openFDA lists 16714-963-01; the shelf stores it as 11 digits, 5-3-2 padded
    item = label("s1", "ABIRATERONE ACETATE", ["16714-963-01", "16714-963-02"])
    with client({"16714-963": item}) as c:
        e = docs.label_for_ndc("16714096301", c)
        assert e == {"doc_id": "s1", "title": "ABIRATERONE", "generic": "ABIRATERONE ACETATE",
                     "version": 6, "label_date": "2026-08-12",
                     "ndcs": ["16714096301", "16714096302"]}
        # same product code, a pack size this label does not list
        assert docs.label_for_ndc("16714096399", c) is None


def tfromshelfranksbyspendandkeepsonelabelpergeneric(tmp_path, monkeypatch):
    monkeypatch.setattr(docs, "PAUSE", 0)
    from pharmacli.store import connect, watch_cost

    db = str(tmp_path / "s.db")
    conn = connect(db)
    watch_cost(conn, [("11111111101", 1.0, 5.0, 10.0),      # spend 10
                      ("22222222201", 5.0, 5.0, 10.0),      # spend 50
                      ("33333333301", 2.0, 5.0, 10.0)])     # spend 20, same generic as 2222
    conn.close()
    labels = {"11111-1111": label("a", "ALPHA", ["11111-1111-01"]),
              "22222-2222": label("b", "BETA", ["22222-2222-01"]),
              "33333-3333": label("c", "BETA", ["33333-3333-01"])}
    with client(labels) as c:
        corpus = docs.from_shelf(db, 2, c, docs_dir=str(tmp_path / "pdfs"))
    assert [e["doc_id"] for e in corpus["inserts"]] == ["b", "a"]
    assert (tmp_path / "pdfs" / "b.pdf").read_bytes() == PDF
    assert [b["doc_id"] for b in corpus["business"]] == [b["doc_id"] for b in docs.BUSINESS]
    assert corpus["business"][0]["sha256"] == hashlib.sha256(PDF).hexdigest()


def tdownloadrejectsanerrorpage(tmp_path):
    with client({}, pdf=b"<html>maintenance</html>") as c:
        assert docs.download("https://example.invalid/x.pdf", tmp_path / "x.pdf", c) is None
    assert not (tmp_path / "x.pdf").exists()


def trefreshpicksupnewerlabelsandchangeddocuments(tmp_path):
    corpus = {"inserts": [{"doc_id": "s1", "title": "A", "generic": "A", "version": 5,
                           "label_date": "2025-01-01", "ndcs": []}],
              "business": [{"doc_id": "law", "url": "https://example.invalid/law.pdf",
                            "sha256": "old", "title": "Law", "jurisdiction": "NY"}]}
    with client({"s1": label("s1", "ALPHA", ["11111-1111-01"], version="6")}) as c:
        changed = docs.refresh(corpus, c, docs_dir=str(tmp_path))
    assert changed == ["s1", "law"]
    assert corpus["inserts"][0]["version"] == 6 and corpus["inserts"][0]["label_date"] == "2026-08-12"
    assert corpus["business"][0]["sha256"] == hashlib.sha256(PDF).hexdigest()


def trefreshskipsalabelwhoselookupfails(tmp_path):
    corpus = {"inserts": [
        {"doc_id": "down", "title": "A", "generic": "A", "version": 1,
         "label_date": "2025-01-01", "ndcs": []},
        {"doc_id": "s1", "title": "B", "generic": "B", "version": 5,
         "label_date": "2025-01-01", "ndcs": []}],
        "business": []}

    def handler(req):
        if req.url.host == "api.fda.gov":
            if 'set_id:"down"' in req.url.params["search"]:
                raise httpx.ConnectError("down", request=req)
            return httpx.Response(200, json={"results": [label("s1", "BETA",
                                                                ["11111-1111-01"], version="6")]})
        return httpx.Response(200, content=PDF)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        changed = docs.refresh(corpus, c, docs_dir=str(tmp_path))
    assert changed == ["s1"]


def tfetchleavesachangeddocumentforrefresh(tmp_path):
    (tmp_path / "law.pdf").write_bytes(b"%PDF-old")
    corpus = {"inserts": [], "business": [{"doc_id": "law", "url": "https://example.invalid/law.pdf",
                                           "sha256": "old", "title": "Law", "jurisdiction": "NY"}]}
    with client({}) as c:
        assert docs.fetch(corpus, c, docs_dir=str(tmp_path)) == ["law"]
    assert (tmp_path / "law.pdf").read_bytes() == b"%PDF-old"        # refresh diffs against it


def tdocsfetchwithoutacorpussaysso(tmp_path, monkeypatch):
    from pharmacli.cli import EXIT_BADIN, main

    monkeypatch.chdir(tmp_path)
    assert main(["docs", "fetch"]) == EXIT_BADIN


def tdocsevalwithoutananswerkeysaysso(tmp_path, monkeypatch):
    from pharmacli.cli import EXIT_BADIN, main

    monkeypatch.chdir(tmp_path)
    assert main(["docs", "eval", "--qa", "missing.jsonl"]) == EXIT_BADIN


def trefreshkeepsthepreviouslabelandrestoresonfailure(tmp_path):
    corpus = {"inserts": [{"doc_id": "s1", "title": "A", "generic": "A", "version": 5,
                           "label_date": "2025-01-01", "ndcs": []}], "business": []}
    (tmp_path / "s1.pdf").write_bytes(b"%PDF-old")
    newer = {"s1": label("s1", "ALPHA", ["11111-1111-01"], version="6")}
    with client(newer) as c:
        assert docs.refresh(corpus, c, docs_dir=str(tmp_path)) == ["s1"]
    assert (tmp_path / "s1.prev.pdf").read_bytes() == b"%PDF-old"    # kept for the digest
    assert (tmp_path / "s1.pdf").read_bytes() == PDF

    corpus["inserts"][0]["version"] = 5
    with client(newer, pdf=b"<html>down</html>") as c:
        assert docs.refresh(corpus, c, docs_dir=str(tmp_path)) == []
    assert (tmp_path / "s1.pdf").read_bytes() == PDF                 # a failed download keeps the label
