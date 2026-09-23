import json

from conftest import FakeChroma, hit, userag

from pharmacli import rageval


def thitrankcoverspages():
    retrieved = [{"doc_id": "met", "page_start": 1, "page_end": 2},
                 {"doc_id": "met", "page_start": 4, "page_end": 5}]
    assert rageval.hit_rank(retrieved, [{"doc_id": "met", "page": 5}]) == 2
    assert rageval.hit_rank(retrieved, [{"doc_id": "other", "page": 1}]) is None


def tfactrecallignorescommasandcase():
    assert rageval.fact_recall("Max dose is 2550 MG per day.", ["2,550 mg", "per day"]) == 1.0
    assert rageval.fact_recall("Max dose is 2000 mg.", ["2,550 mg"]) == 0.0
    assert rageval.fact_recall("anything", []) is None


def tfaithfulnesscountssupportedsentences():
    def vec(text):
        return [1.0, 0.0] if "lactic" in text.lower() else [0.0, 1.0]

    score = userag(lambda r: rageval.faithfulness(
        r, "Lactic acidosis can be fatal [1]. The moon is cheese [1].",
        ["METFORMIN · lactic acidosis warning"]), FakeChroma(), embedding=vec)
    assert score == 0.5


ROWS = [
    {"id": "d1", "q": "what does the metformin label say about renal impairment",
     "expect_lane": "documents", "gold": [{"doc_id": "met", "page": 4}], "facts": ["egfr"],
     "label_version": 3, "reviewed": True},
    {"id": "d2", "q": "what is the metformin boxed warning", "expect_lane": "documents",
     "gold": [{"doc_id": "met", "page": 9}], "facts": [], "label_version": 3, "reviewed": True},
    {"id": "old", "q": "what is the zituvimet storage", "expect_lane": "documents",
     "gold": [{"doc_id": "sitamet", "page": 30}], "facts": [],
     "label_version": 5, "reviewed": True},
    {"id": "p1", "q": "what am I losing money on", "expect_lane": "price", "gold": [],
     "facts": [], "label_version": None, "reviewed": True},
    {"id": "r1", "q": "should I give my patient metformin", "expect_lane": "refuse", "gold": [],
     "facts": [], "label_version": None, "reviewed": True},
]


def tretrievalonlyrunscoresranksandskipsstaleandpricerows():
    chroma = FakeChroma([hit(1, page=4), hit(2, page=5)])
    result = userag(lambda r: rageval.run(r, ROWS, "section", 2, retrieval_only=True), chroma)
    rows = {r["id"]: r for r in result["rows"]}
    assert rows["d1"]["rank"] == 1 and rows["d2"]["rank"] is None
    assert rows["old"] == {"id": "old", "expect": "documents", "lane": "documents", "stale": True}
    assert "rank" not in rows["p1"]                     # price rows never reach retrieval
    assert result["summary"] == {"rows": 4, "stale": 1, "routing": 4, "answerable": 2,
                                 "hit": 1, "mrr": 0.5}


def tfullrunscoresrefusalsandfacts():
    chroma = FakeChroma([hit(1, page=4, text="METFORMIN · eGFR below 30 contraindicated")])
    result = userag(lambda r: rageval.run(r, ROWS, "section", 1, retrieval_only=False), chroma,
                    reply="Contraindicated when eGFR is below 30 [1].")
    s = result["summary"]
    assert (s["refuse_rows"], s["refused_correctly"], s["false_refusals"]) == (1, 1, 0)
    assert s["grounded"] == 2 and s["fact_recall"] == 1.0 and s["errors"] == 0
    rows = {r["id"]: r for r in result["rows"]}
    assert rows["d1"]["answer"] == "Contraindicated when eGFR is below 30 [1]."
    assert rows["r1"]["reason"] == "patient_specific"
    assert json.dumps(result)                           # the --json dump must serialise


def tcompareshowscountsandflips():
    def run(ranks):
        rows = [{"id": i, "expect": "documents", "lane": "documents", "rank": rk}
                for i, rk in ranks.items()]
        return {"config": {"chunking": "x", "k": 5}, "rows": rows,
                "summary": rageval.summarize(rows)}

    before = run({"a": None, "b": 1, "c": None})
    after = run({"a": 2, "b": None, "c": 1})
    before["config"]["chunking"], after["config"]["chunking"] = "fixed", "section"
    out = rageval.compare(before, after)
    assert out[0].split() == ["metric", "fixed", "k=5", "section", "k=5"]
    assert any(line.split() == ["hit", "1/3", "2/3"] for line in out)
    assert "flipped +2 / -1" in out
    assert "  now right: a, c" in out and "  now wrong: b" in out


def tcompareflipsfulrunrowswithoutfacts():
    def fullrun(grounded_dict):
        # full run rows with refused/grounded/fact_recall keys
        rows = [{"id": i, "expect": "documents", "lane": "documents",
                 "refused": False, "grounded": g, "fact_recall": None, "error": False}
                for i, g in grounded_dict.items()]
        return {"config": {"chunking": "section", "k": 5}, "rows": rows,
                "summary": rageval.summarize(rows)}

    before = fullrun({"x": False})
    after = fullrun({"x": True})
    out = rageval.compare(before, after)
    assert "flipped +1 / -0" in out
    assert "  now right: x" in out
