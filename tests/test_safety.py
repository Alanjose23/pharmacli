from datetime import date

import httpx
import pytest

from conftest import RAG_CORPUS, FakeChroma, hit, makepdf, userag
from pharmacli import safety

RECALLS = [
    {"recall_number": "D-1", "classification": "Class II", "status": "Ongoing",
     "report_date": "20260901", "recalling_firm": "Acme", "reason_for_recall": "dissolution",
     "product_description": "Drug X tablets, NDC 16714-963-01", "code_info": "Lot 123",
     # openfda lists the other strength too; the text says which one was recalled
     "openfda": {"product_ndc": ["16714-963", "16714-999"]}},
    {"recall_number": "D-2", "classification": "Class I", "status": "Ongoing",
     "report_date": "20260801", "recalling_firm": "Beta", "reason_for_recall": "sterility",
     "product_description": "Injection", "code_info": "Lot 9",
     "openfda": {"product_ndc": ["0904-6950"]}},
    {"recall_number": "D-3", "classification": "Class I", "status": "Ongoing",
     "report_date": "20260905", "recalling_firm": "Gamma", "reason_for_recall": "label",
     "product_description": "NDC 11111-2222-33", "code_info": "", "openfda": {}},
]


def tfetchrecallspagesthroughthedatewindow():
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        batch = RECALLS[:2] if req.url.params["skip"] == "0" else RECALLS[2:]
        return httpx.Response(200, json={"meta": {"results": {"total": 3}}, "results": batch})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        got = safety.fetch_recalls(c, days=10, today=date(2026, 9, 15))
    assert [r["recall_number"] for r in got] == ["D-1", "D-2", "D-3"]
    assert seen[0]["search"] == "report_date:[20260905 TO 20260915]"
    assert seen[1]["skip"] == "2"


def trecallsmatchpackagesandproductsclassifirst():
    shelf = {"16714096301": 12.0, "16714096302": 1.0, "16714099901": 5.0,
             "00904695061": None, "55555555555": 3.0}
    hits = safety.shelf_matches(RECALLS, shelf)
    # D-1: the named product in every pack size, not the other strength openfda
    # also lists; D-2 names no NDC, so its openfda product code is used
    assert [(rec["recall_number"], found) for rec, found in hits] == [
        ("D-2", ["00904695061"]), ("D-1", ["16714096301", "16714096302"])]
    text = "\n".join(safety.recall_lines(hits, shelf, checked=3, days=90))
    assert "16714096301 (on hand 12)" in text and "Lot 123" in text
    assert text.endswith(safety.LOTS)
    assert safety.recall_lines([], shelf, 3, 90)[0].startswith("clean:")


def tlabeldigestputsboxedwarningfirst(tmp_path):
    pytest.importorskip("pymupdf")
    old, new = tmp_path / "old.pdf", tmp_path / "new.pdf"
    makepdf(old, ["2.1 Dosage", "take 10 mg daily", "5.1 Lactic Acidosis", "rare"],
            ["16 HOW SUPPLIED", "bottles of 30"])
    makepdf(new, ["WARNING: LACTIC ACIDOSIS", "can be fatal", "2.1 Dosage", "take 20 mg daily",
                  "5.1 Lactic Acidosis", "rare"])
    out = safety.label_digest(old, new)
    assert out[0] == "added section: WARNING: LACTIC ACIDOSIS"
    assert ["changed: 2.1 Dosage", "  -take 10 mg daily", "  +take 20 mg daily"] == out[1:4]
    assert "removed section: 16 HOW SUPPLIED" in out
    assert not any("5.1" in line for line in out)                    # unchanged, not shown


def thandouttakesthereal17notthecontentsline(tmp_path):
    pytest.importorskip("pymupdf")
    corpus = {"inserts": [RAG_CORPUS["inserts"][0]], "business": []}
    makepdf(tmp_path / "met.pdf",
            ["17 PATIENT COUNSELING INFORMATION", "1 INDICATIONS AND USAGE", "treats diabetes"],
            ["17 PATIENT COUNSELING INFORMATION", "Lactic acidosis: explain the risk-",
             "factors to patients.", "Hypoglycemia: tell patients."])
    md = safety.handout("metformin", corpus, docs_dir=str(tmp_path))
    assert "Lactic acidosis: explain the risk-factors to patients." in md
    assert ("Source: METFORMIN HYDROCHLORIDE · 17 PATIENT COUNSELING INFORMATION · p.2-2"
            " · label 2026-01-02") in md
    assert md.rstrip().endswith(f"_{safety.COUNSEL}_")

    makepdf(tmp_path / "met.pdf", ["2.1 Dosage", "take with meals"])
    with pytest.raises(LookupError, match="no section 17"):
        safety.handout("metformin", corpus, docs_dir=str(tmp_path))
    with pytest.raises(LookupError, match="no label"):
        safety.handout("warfarin", corpus, docs_dir=str(tmp_path))


def tinteractionsloopspairsandlistsunknowns():
    async def go(r):
        return [ln async for ln in safety.interactions(r, ["metformin", "zituvimet", "unobtainium"])]

    lines = userag(go, FakeChroma([hit(1)]), reply="Monitor blood glucose [1].")
    assert lines[0] == "not checked: unobtainium (no label in the corpus)"
    assert [ln for ln in lines if ln.startswith("== ")] == [
        "== metformin + zituvimet", "== metformin + unobtainium", "== zituvimet + unobtainium"]
    assert "Monitor blood glucose [1]." in lines and lines[-1] == safety.CAVEAT

    async def one(r):
        return [ln async for ln in safety.interactions(r, ["metformin", "zituvimet"])]

    with pytest.raises(ConnectionError):
        userag(one, FakeChroma([hit(1)]), down=True)


def tinteractionscapsthedruglist():
    from pharmacli.cli import EXIT_BADIN, main

    assert main(["interactions", "metformin"]) == EXIT_BADIN
    assert main(["interactions", *"abcdefg"]) == EXIT_BADIN


def trecallsmatchanuploadedshelfwithoutthedatabase(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "fetch_recalls", lambda client, days=90: RECALLS)
    db = tmp_path / "never.db"
    upload = ({"16714096301": 1.0}, {"16714096301": (12.0, 30.0)})
    text = "\n".join(safety.shelf_recalls(str(db), shelf=upload))
    assert "16714096301 (on hand 12)" in text
    assert not db.exists()              # an uploaded shelf needs no database
