# What a pharmacist reviews, with a stand-in model: pair categories and the
# checks that keep a wrong answer from looking like a result, the cache and the
# queue derived from it, the report with its counts, indicator-matched label
# sections, shelf checks, and decisions. No Ollama, no Chroma, no network.
import json
import re
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from conftest import CLASSES, PATIENT_CORPUS, FakeChroma, label, row, signin, userag

pytest.importorskip("pymupdf")

from pharmacli import patients, rageval, review  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
A, B, C, NONE = review.CATEGORIES
DRUGS = review.drugs(PATIENT_CORPUS)
ALPHA, BETA, GAMMA, DELTA = (DRUGS[k] for k in ("alphazine", "betamol", "gammavir", "deltaquin"))


def model(answers: dict, sent: list | None = None):
    # the stand-in: answers by (label, other drug); a prompt it has no answer
    # for is a model call the test did not expect, and fails it
    def reply(body):
        if sent is not None:
            sent.append(body)
        user = body["messages"][1]["content"]
        key = (re.search(r"Label: (.+)", user).group(1),
               re.search(r"Other drug: (.+)", user).group(1))
        return answers[key]
    return reply


ALPHA_ON_BETA = ("alphazine hydrochloride", "betamol")
ALPHA_ON_GAMMA = ("alphazine hydrochloride", "gammavir sodium")
ALPHA_ON_DELTA = ("alphazine hydrochloride", "deltaquin and betamol")
GAMMA_ON_DELTA = ("gammavir sodium", "deltaquin and betamol")
DELTA_ON_BETA = ("deltaquin and betamol", "betamol")
ANSWERS = {ALPHA_ON_BETA: "SENTENCE: 1\nCATEGORY: A", ALPHA_ON_GAMMA: "SENTENCE: 1\nCATEGORY: B",
           ALPHA_ON_DELTA: "SENTENCE: 1\nCATEGORY: A", GAMMA_ON_DELTA: "SENTENCE: 1\nCATEGORY: C",
           DELTA_ON_BETA: "SENTENCE: 1\nCATEGORY: D"}


def check(a, b, labels, answers=ANSWERS, sent=None, **kw):
    return userag(lambda r: review.check(r, a, b, labels), FakeChroma(), corpus=PATIENT_CORPUS,
                  reply=model(answers, sent), llm="standin", **kw)


# ── reading a label ──────────────────────────────────────────────────────────

def tsectionsskipthehighlightsandcontentsandkeeplistitems(labels, tmp_path):
    found = review._label("alpha", 2, str(tmp_path))
    assert [m for m, _, _ in found] == ["1", "2", "4", "5", "7", "8", "8", "8", "17", "11", "16"]
    text = " ".join(t for _, _, lines in found for _, t in lines)
    assert "never quoted" not in text                        # the Highlights
    by_heading = {h: [t for _, t in lines] for _, h, lines in found}
    assert by_heading["4 CONTRAINDICATIONS"] == ["Known hypersensitivity to the product.",
                                                  "Do not use alphazine with betamol."]
    assert by_heading["8.6 Renal Impairment"][0].startswith("Reduce the dose to 5 mg")

    # a numbered list and a strength in capitals are text, not the start of a section
    lines = [(1, "4 CONTRAINDICATIONS"), (1, "Do not use in:"), (1, "1. Severe testitis."),
             (1, "2. Children under 2."), (1, "5 MG TABLETS are scored."),
             (2, "5 WARNINGS AND PRECAUTIONS"), (2, "5.1 Liver"), (2, "Monitor the liver.")]
    got = review.sections(lines)
    assert [(m, h, len(body)) for m, h, body in got] == [
        ("4", "4 CONTRAINDICATIONS", 4), ("5", "5.1 Liver", 1)]
    # an older label has no numbered sections and comes back whole
    old = [(1, "CONTRAINDICATIONS"), (1, "Do not use with betamol."), (2, "1. Take daily.")]
    assert review.sections(old) == [(review.WHOLE, "", old)]


def ttermscoverthenametheclassandhowlabelswriteit():
    name, kind = review.terms(BETA, CLASSES)
    assert name.search("gabapentinoids") is None and name.search("Betamol-containing")
    for written in ("strong CYP3A4 inducers", "inducers of CYP3A4", "drugs that induce CYP 3A4",
                    "Cytochrome P450 3A4 inducers"):
        assert kind.search(written), written
    assert not kind.search("CYP3A4 inhibitors") and not kind.search("CYP2D6 inducers")
    _, kind = review.terms(ALPHA, CLASSES)
    assert kind.search("testase inhibitors") and kind.search("a TSI") and kind.search("TSIs")
    assert not kind.search("TSIX") and not kind.search("protestase inhibitor")
    # a combination is named by each of its ingredients
    name, kind = review.terms(DELTA, CLASSES)
    assert name.search("deltaquin") and name.search("betamol") and kind.search("quinolines")
    # no class on file: the name can be searched, the class cannot
    assert review.terms(GAMMA, {"drugs": {}})[1] is None


def talabelsownnameisnotamentionoftheotherdrug(tmp_path):
    corpus = {"inserts": [
        {"doc_id": "lp", "title": "LOSARTAN POTASSIUM", "generic": "LOSARTAN POTASSIUM",
         "version": 1, "label_date": "2026-01-01", "ndcs": []},
        {"doc_id": "kcl", "title": "POTASSIUM CHLORIDE", "generic": "POTASSIUM CHLORIDE",
         "version": 1, "label_date": "2026-01-01", "ndcs": []}], "business": []}
    label(tmp_path, "lp", dose=["Losartan potassium tablets are taken daily."],
          inter=["Avoid potassium supplements with losartan potassium."])
    lp, kcl = (review.drugs(corpus)[k] for k in ("losartan", "potassium"))
    found, _ = review.mentions(lp.labels[0], review.terms(lp, {})[0], review.terms(kcl, {})[0],
                               None, str(tmp_path))
    assert [s["text"] for s in found] == ["Avoid potassium supplements with losartan potassium."]


# ── categories, and the checks on the model's answer ────────────────────────

def teachcategoryrestsonthelabelsownsentence(labels):
    sent: list = []
    got = check(ALPHA, BETA, labels, sent=sent)
    assert got["status"] == A == "Do not use together" and got["reason"] == ""
    assert got["quotes"] == [{"category": A, "quote": "Do not use alphazine with betamol.",
                              "cite": "ALPHAZINE · 4 CONTRAINDICATIONS · p.2 · label 2026-01-02"}]
    assert len(sent) == 1                    # betamol's label names neither: no call for it
    assert got["searched"] == [
        "ALPHAZINE · sections 2, 4, 5, 7 searched · p.2 · label 2026-01-02",
        "BETAMOL · sections 2, 4, 5, 7 searched · p.2 · label 2026-02-03"]

    got = check(ALPHA, GAMMA, labels)
    assert got["status"] == B == "Avoid or adjust the dose"
    assert got["quotes"][0]["quote"] == \
        "Reduce the alphazine dose by half when it is taken with gammavir."
    assert got["quotes"][0]["cite"] == "ALPHAZINE · 7.1 Other Drugs · p.2 · label 2026-01-02"
    assert len(got["searched"]) == 3         # gammavir has two labels, and both were read

    got = check(GAMMA, DELTA, labels)
    assert got["status"] == C == "Monitor"
    assert got["quotes"][0]["cite"] == "GAMMAVIR · 5.1 Liver Injury · p.2 · label 2026-03-04"

    # deltaquin's label is for a combination with betamol, so what alphazine's
    # label says about betamol is put to the model too -- and first, as the
    # more severe source
    sent = []
    got = check(ALPHA, DELTA, labels, sent=sent)
    assert "[1] Do not use alphazine with betamol.\n[2] Monitor liver" in \
        sent[0]["messages"][1]["content"]
    assert got["status"] == A and got["quotes"][0]["quote"] == "Do not use alphazine with betamol."


def tnointeractionstatedcomesfromthesearchneverfromthemodel(labels, tmp_path):
    sent: list = []
    got = check(BETA, GAMMA, labels, sent=sent)
    assert got["status"] == NONE == "No interaction stated in the labels"
    assert sent == [] and got["quotes"] == []                # no model call at all
    # the citation is what was searched, in every label of both drugs
    assert [s.split(" · ")[0] for s in got["searched"]] == ["BETAMOL", "GAMMAVIR", "GAMMAVIR ER"]
    assert review.NOT_SAFE.endswith("which is not the same as safe.")

    # the model saying "no interaction" where a label names the drug is not believed
    got = check(ALPHA, BETA, labels, {ALPHA_ON_BETA: "CATEGORY: D\nSENTENCE: 1"})
    assert got["status"] == review.MANUAL and "read no interaction" in got["reason"]
    # a label naming only the other drug's class is still a mention: the model is asked
    label(tmp_path, "gamma",
          warn=["Strong CYP3A4 inducers lower gammavir levels and should be avoided."])
    review._label.cache_clear()
    got = check(GAMMA, BETA, labels, {("gammavir sodium", "betamol"): "CATEGORY: B\nSENTENCE: 1"})
    assert got["status"] == B and "CYP3A4 inducers" in got["quotes"][0]["quote"]
    # and the category cannot be milder than that sentence's own words ("should be avoided")
    got = check(GAMMA, BETA, labels, {("gammavir sodium", "betamol"): "CATEGORY: C\nSENTENCE: 1"})
    assert got["status"] == review.MANUAL and "or its class is worded as" in got["reason"]


def teachfailedcheckendsinmanualreview(labels):
    wrong = {"not the form asked for": "I think these two interact quite a lot.",
             "a category outside the set": "CATEGORY: E\nSENTENCE: 1",
             "a sentence it was not given": "CATEGORY: A\nSENTENCE: 7",
             "no interaction, against the search": "CATEGORY: D\nSENTENCE: 1"}
    for what, reply in wrong.items():
        got = check(ALPHA, BETA, labels, {ALPHA_ON_BETA: reply})
        assert got["status"] == review.MANUAL and got["reason"], what
        # no category is claimed, but the sentence that was found is still shown, cited
        assert got["quotes"] == [{
            "category": None, "quote": "Do not use alphazine with betamol.",
            "cite": "ALPHAZINE · 4 CONTRAINDICATIONS · p.2 · label 2026-01-02"}], what
    # the chosen sentence must name the other drug or its class
    name, kind = review.terms(BETA, CLASSES)
    off = review.verdict("CATEGORY: A\nSENTENCE: 1", [{"text": "Take with food."}], BETA, name,
                         kind)
    assert "does not name betamol" in off["manual"]
    # no class on file for a drug: an empty search is not "nothing stated"
    bare = review.Labels(PATIENT_CORPUS, {"drugs": {"betamol": CLASSES["drugs"]["betamol"]}},
                         labels.docs_dir)
    got = check(BETA, GAMMA, bare)
    assert got["status"] == review.MANUAL and "no pharmacologic class is on file" in got["reason"]


def tthelabelsownwordingcatchesthemodelgoingmilderorharsher(labels):
    assert [review.worded(t) for t in (
        "Zeta is contraindicated with omega.", "Do not administer zeta with omega.",
        "Avoid omega.", "Reduce the zeta dose by half.", "Double the usual dose.",
        "Dosage adjustment is recommended.", "Reserve concomitant prescribing for ...",
        "Monitor serum potassium.", "Omega may increase zeta levels.")] == [
            0, 0, 1, 1, 1, 1, 1, 2, 2]
    # the model reads a forbidding sentence as "monitor": a pharmacist decides, not the model
    got = check(ALPHA, BETA, labels, {ALPHA_ON_BETA: "SENTENCE: 1\nCATEGORY: C"})
    assert got["status"] == review.MANUAL and "worded as \"do not use together\"" in got["reason"]
    # ... and reads a dose change as "monitor"
    got = check(ALPHA, GAMMA, labels, {ALPHA_ON_GAMMA: "SENTENCE: 1\nCATEGORY: C"})
    assert got["status"] == review.MANUAL and "avoid or adjust the dose" in got["reason"]
    # the model calls a monitoring sentence "do not use together": not shown as that either
    got = check(GAMMA, DELTA, labels, {GAMMA_ON_DELTA: "SENTENCE: 1\nCATEGORY: A"})
    assert got["status"] == review.MANUAL and "does not say that" in got["reason"]
    # the model picks the milder of two sentences that name the drug's ingredient
    got = check(ALPHA, DELTA, labels, {ALPHA_ON_DELTA: "SENTENCE: 2\nCATEGORY: C"})
    assert got["status"] == review.MANUAL
    # what the pharmacist is shown: the forbidding sentence first, with its page
    assert got["quotes"][0]["quote"] == "Do not use alphazine with betamol."


def twhentwolabelsdisagreethemoresevereonestands(labels, tmp_path):
    label(tmp_path, "alpha", warn=["Monitor closely when alphazine is given with betamol."])
    label(tmp_path, "beta", contra=["Betamol must not be used with alphazine."])
    answers = {ALPHA_ON_BETA: "CATEGORY: C\nSENTENCE: 1",
               ("betamol", "alphazine hydrochloride"): "CATEGORY: A\nSENTENCE: 1"}
    got = check(ALPHA, BETA, labels, answers)
    assert got["status"] == A
    assert [(q["category"], q["cite"].split(" · ")[0]) for q in got["quotes"]] == [
        (A, "BETAMOL"), (C, "ALPHAZINE")]                    # both shown, the severe one first
    # one label failing a check outranks a clean answer from the other
    answers[("betamol", "alphazine hydrochloride")] = "no idea"
    got = check(ALPHA, BETA, labels, answers)
    assert got["status"] == review.MANUAL
    assert [(q["category"], q["cite"].split(" · ")[0]) for q in got["quotes"]] == [
        (C, "ALPHAZINE"), (None, "BETAMOL")]


def tthesentenceafteramentionisquotedwithit(labels, tmp_path):
    label(tmp_path, "alpha", inter=["Gammavir raises alphazine levels.",
                                    "Reduce the alphazine dose by half.", "Unrelated text."])
    got = check(ALPHA, GAMMA, labels)
    assert got["quotes"][0]["quote"] == \
        "Gammavir raises alphazine levels. Reduce the alphazine dose by half."


# ── no patient field in any prompt ───────────────────────────────────────────

SECRET = {"name": "Zebulon Quixote-Canary", "dob": "1931-07-13",
          "drugs": "ALPHAZINE hcl 10mg qd; Betamol (pt says prn); gammavir",
          "egfr": "41.7", "allergies": "canary-feather dander"}


def add(conn, raw=SECRET) -> str:
    clean, errors = patients.clean(raw)
    assert not errors, errors
    return patients.add_patient(conn, clean, "rph")


def work(db, labels, answers=ANSWERS, sent=None, **kw):
    return userag(lambda r: review.work(r, db, labels, **kw), FakeChroma(), corpus=PATIENT_CORPUS,
                  reply=model(answers, sent), llm="standin")


def tnopatientfieldreachesaprompt(labels, tmp_path):
    db = str(tmp_path / "p.db")
    with closing(patients.connect(db)) as conn:
        pid = add(conn)
    sent: list = []
    assert work(db, labels, sent=sent) == 3                  # the three pairs of the three drugs
    assert len(sent) == 2 and all(len(b["messages"]) == 2 for b in sent)
    everything = json.dumps(sent)
    assert "Label: alphazine hydrochloride" in everything    # the corpus's name for the drug
    for secret in ("Zebulon", "Quixote", "Canary", "1931", "41.7", "canary-feather", "10mg qd",
                   "pt says prn", "hcl", pid):
        assert secret not in everything, f"{secret!r} was sent to the model"
    # and build_prompt has no parameter a patient field could arrive through
    assert review.build_prompt.__code__.co_varnames[:3] == ("drug", "other", "shown")


# ── the cache, and the queue derived from it ─────────────────────────────────

def tthecacheanswersuntilalabelthepromptorthemodelchanges(labels, tmp_path):
    db = str(tmp_path / "p.db")
    with closing(patients.connect(db)) as conn:
        add(conn, SECRET | {"drugs": "alphazine; betamol"})
    sent: list = []
    assert work(db, labels, sent=sent) == 1 and len(sent) == 1           # a miss: asked
    assert work(db, labels, sent=sent) == 0 and len(sent) == 1           # a hit: not asked again
    with closing(patients.connect(db)) as conn:
        assert review.cached(conn, ALPHA, BETA, labels, "standin")["status"] == A
        assert review.cached(conn, BETA, ALPHA, labels, "standin")["status"] == A    # either order
        # another model, or changed class terms, is a different question
        assert review.cached(conn, ALPHA, BETA, labels, "another-model") is None
        other = review.Labels(PATIENT_CORPUS, CLASSES | {"aliases": {}}, labels.docs_dir)
        assert review.cached(conn, ALPHA, BETA, other, "standin") is None

    # `docs refresh` brings a new version of one label: its pairs are checked again
    newer = json.loads(json.dumps(PATIENT_CORPUS))
    newer["inserts"][1]["version"] += 1                                   # betamol
    refreshed = review.Labels(newer, CLASSES, labels.docs_dir)
    with closing(patients.connect(db)) as conn:
        assert [(a.key, b.key) for a, b in review.pending(conn, refreshed, "standin")] == [
            ("alphazine", "betamol")]
    assert work(db, refreshed, sent=sent) == 1 and len(sent) == 2
    with closing(patients.connect(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pair").fetchone()[0] == 2    # old answer kept


def tthequeueresumesafteraninterruption(labels, tmp_path):
    db = str(tmp_path / "p.db")
    with closing(patients.connect(db)) as conn:
        add(conn)
        add(conn, SECRET | {"name": "Ada Example", "drugs": "betamol; gammavir"})   # a shared pair
        assert len(review.pending(conn, labels, "standin")) == 3
    sent: list = []
    progress = []
    assert work(db, labels, sent=sent, limit=1,
                progress=lambda a, b, got, left: progress.append((a.key, b.key, left))) == 1
    assert progress == [("alphazine", "betamol", 2)]
    with closing(patients.connect(db)) as conn:
        assert [(a.key, b.key) for a, b in review.pending(conn, labels, "standin")] == [
            ("alphazine", "gammavir"), ("betamol", "gammavir")]

    # the model goes away mid-run: what was answered is kept, the rest stays pending
    def dies(body):
        raise httpx.ConnectError("ollama went away")
    with pytest.raises(httpx.HTTPError):
        userag(lambda r: review.work(r, db, labels), FakeChroma(), corpus=PATIENT_CORPUS,
               reply=dies, llm="standin")
    with closing(patients.connect(db)) as conn:
        assert len(review.pending(conn, labels, "standin")) == 2
    assert work(db, labels, sent=sent) == 2 and len(sent) == 2      # only the unanswered pair asked
    assert work(db, labels, sent=sent) == 0


def tthequeuewaitswhilealivequestionisbeinganswered(labels, tmp_path):
    db = str(tmp_path / "p.db")
    with closing(patients.connect(db)) as conn:
        add(conn, SECRET | {"drugs": "alphazine; betamol"})
    asked = iter([True, True, False])
    polls = []

    def busy():
        polls.append(1)
        return next(asked)

    import asyncio
    real = asyncio.sleep

    async def no_wait(seconds):
        await real(0)

    try:
        asyncio.sleep = no_wait
        assert work(db, labels, busy=busy) == 1
    finally:
        asyncio.sleep = real
    assert len(polls) == 3                                   # it stood aside twice, then ran


def tamissinglabelpdfleavesthepairpendingnotfailed(labels, tmp_path):
    db = str(tmp_path / "p.db")
    with closing(patients.connect(db)) as conn:
        pid = add(conn, SECRET | {"drugs": "alphazine; betamol"})
    (tmp_path / "beta.pdf").unlink()
    assert work(db, labels) == 0
    with closing(patients.connect(db)) as conn:
        rep = review.report(conn, pid, labels, "standin", str(tmp_path / "none.db"))
    assert rep["pending"] == ["alphazine hydrochloride + betamol"]
    # the readings still bring up alphazine's sections; betamol's is a manual review item
    titles = {i["title"]: i["status"] for i in rep["items"] if i["kind"] == "indicator"}
    assert titles["alphazine hydrochloride: renal impairment"] == review.READ
    assert titles["betamol: renal impairment"] == review.MANUAL


# ── the report ───────────────────────────────────────────────────────────────

def report(db, pid, labels, tmp_path):
    with closing(patients.connect(db)) as conn:
        return review.report(conn, pid, labels, "standin", str(tmp_path / "prices.db"))


def tthereportsayshowmanypairsarecheckedandnamestherest(labels, tmp_path):
    db = str(tmp_path / "p.db")
    with closing(patients.connect(db)) as conn:
        pid = add(conn, SECRET | {"drugs": "alphazine; betamol; gammavir; unobtainium",
                                  "egfr": "88"})
    rep = report(db, pid, labels, tmp_path)
    assert (rep["checked"], rep["total"]) == (0, 6)
    assert {i["status"] for i in rep["items"]} == {review.PENDING, review.NOT_CHECKED}
    assert not any("key" in i for i in rep["items"])         # nothing to decide on yet

    work(db, labels, limit=2)
    rep = report(db, pid, labels, tmp_path)
    assert rep["summary"] == ("2 of 6 pairs checked; pending: betamol + gammavir sodium; "
                              "not checked, no label: unobtainium")
    assert rep["pending"] == ["betamol + gammavir sodium"] and rep["not_checked"] == ["unobtainium"]
    # sorted with the most severe first, and what is unresolved above "nothing stated"
    assert [(i["status"], i["title"]) for i in rep["items"]] == [
        (A, "alphazine hydrochloride + betamol"),
        (B, "alphazine hydrochloride + gammavir sodium"),
        (review.NOT_CHECKED, "alphazine hydrochloride + unobtainium"),
        (review.NOT_CHECKED, "betamol + unobtainium"),
        (review.NOT_CHECKED, "gammavir sodium + unobtainium"),
        (review.PENDING, "betamol + gammavir sodium")]
    assert rep["items"][2]["note"] == "unobtainium has no label in the corpus"

    work(db, labels)
    rep = report(db, pid, labels, tmp_path)
    last = rep["items"][-1]
    assert (last["status"], last["note"]) == (NONE, review.NOT_SAFE)
    assert last["quotes"] == [] and len(last["searched"]) == 3    # cited by what was searched
    assert rep["ms"] < 60_000 and "3 of 6 pairs checked" in rep["summary"]
    text = "\n".join(review.report_lines(rep))
    assert "DO NOT USE TOGETHER  alphazine hydrochloride + betamol" in text
    assert "which is not the same as safe" in text and "searched: BETAMOL" in text
    # nowhere does the tool's own wording call anything safe
    assert "safe" not in text.replace("which is not the same as safe", "")
    # an unknown patient is None, for the caller to turn into a 404
    with closing(patients.connect(db)) as conn:
        assert review.report(conn, "0" * 16, labels, "standin", "x.db") is None


def tindicatorsbringupthelabelsownsectionswiththepatientsvalue(labels, tmp_path):
    db = str(tmp_path / "p.db")
    with closing(patients.connect(db)) as conn:
        low = add(conn, SECRET | {"drugs": "alphazine; betamol", "egfr": "38", "alt": "96",
                                  "ast": "20", "pregnant": "yes", "taken": "2026-10-21"})
        fine = add(conn, SECRET | {"name": "Dev Placeholder", "drugs": "alphazine", "egfr": "88",
                                   "alt": "30", "pregnant": "no"})
    items = {i["title"]: i for i in report(db, low, labels, tmp_path)["items"]
             if i["kind"] == "indicator"}
    renal = items["alphazine hydrochloride: renal impairment"]
    assert renal["status"] == review.READ
    assert renal["quotes"] == [{
        "quote": "Reduce the dose to 5 mg when creatinine clearance is below 30 mL/min.",
        "cite": "ALPHAZINE · 8.6 Renal Impairment · p.3 · label 2026-01-02"}]
    # the patient's value sits beside it, with the trigger and its source, marked provisional
    assert "eGFR 38 mL/min/1.73 m2 is below 60 (reading of 2026-10-21)" in renal["note"]
    assert "provisional" in renal["note"] and "KDIGO 2024" in renal["note"]
    assert items["alphazine hydrochloride: hepatic impairment"]["quotes"][0]["quote"] == \
        "Avoid alphazine in severe hepatic impairment."
    assert "ALT 96 U/L is above 40" in items["alphazine hydrochloride: hepatic impairment"]["note"]
    assert items["alphazine hydrochloride: pregnancy"]["quotes"][0]["cite"].startswith(
        "ALPHAZINE · 8.1 Pregnancy · p.3")
    assert items["betamol: pregnancy"]["quotes"][0]["quote"] == "No human data are available."
    # a label with no such section is not silence: it needs a person to read it
    missing = items["betamol: renal impairment"]
    assert missing["status"] == review.MANUAL and missing["quotes"] == []
    assert "has no section headed renal impairment" in missing["note"]
    # nothing notable: nothing is brought up
    assert not [i for i in report(db, fine, labels, tmp_path)["items"] if i["kind"] == "indicator"]
    for trigger in review.TRIGGERS:                          # every trigger carries a source
        assert trigger["source"] and trigger["topic"] in review.TOPICS


def tshelfcheckscitetheirnadacfileandrecallnumber(labels, tmp_path, monkeypatch):
    from pharmacli.models.medicine import Medicine
    from pharmacli.store import connect, insert

    prices = str(tmp_path / "prices.db")
    conn = connect(prices)
    insert(conn, [Medicine.arow(row("22222222201", "10.00000", "2026-01-14", rate="B",
                                    generic="4.00000"), 1)])
    conn.close()
    items, note = review.generic_items([ALPHA, BETA], prices)
    assert note == "" and [i["title"] for i in items] == ["betamol: a cheaper generic is priced"]
    assert items[0]["quotes"] == [{
        "quote": "DRUG 22222222201 (NDC 22222222201): brand $10.00000 a unit, generic $4.00000",
        "cite": "NADAC file 2026-01-14"}]
    assert "not a substitution" in items[0]["note"]
    # no price panel is said, not passed over -- and the file is not created by asking
    items, note = review.generic_items([BETA], str(tmp_path / "absent.db"))
    assert items == [] and "no price panel" in note and not (tmp_path / "absent.db").exists()

    recall = {"recall_number": "D-0042-2027", "classification": "Class II",
              "report_date": "20261101", "recalling_firm": "Acme", "reason_for_recall":
              "Failed dissolution.", "product_description": "Betamol tablets, NDC 22222-2222-01",
              "code_info": "Lot 7", "openfda": {}}
    monkeypatch.setattr(review, "_recalls", lambda day: (recall,))
    items, note = review.recall_items([ALPHA, BETA])
    assert note == "" and items[0]["title"] == "betamol: FDA recall D-0042-2027"
    assert items[0]["quotes"][0]["cite"] == "FDA recall D-0042-2027 · reported 20261101 · Acme"
    assert "lot" in items[0]["note"]

    def down(day):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(review, "_recalls", down)
    assert review.recall_items([BETA]) == (
        [], "recalls not checked: openFDA could not be reached (ConnectError)")


def tdecisionsareappendedandtheneweststands(labels, tmp_path):
    db = str(tmp_path / "p.db")
    with closing(patients.connect(db)) as conn:
        pid = add(conn, SECRET | {"drugs": "alphazine; betamol; gammavir", "egfr": "88"})
    work(db, labels)
    rep = report(db, pid, labels, tmp_path)
    first, second = rep["items"][0]["key"], rep["items"][1]["key"]
    assert all(i["decision"] is None for i in rep["items"])            # each starts pending
    with closing(patients.connect(db)) as conn:
        patients.decide(conn, pid, first, "accepted", "", "rph")
        patients.decide(conn, pid, second, "overridden", "dose already halved", "rph")
        assert patients.decision_counts(conn) == {"accepted": 1, "overridden": 1}
        patients.decide(conn, pid, first, "overridden", "prescriber consulted", "amy")
        assert patients.decision_counts(conn) == {"accepted": 0, "overridden": 2}
        # nothing was replaced: three decisions, three rows
        assert conn.execute("SELECT COUNT(*) FROM decision").fetchone()[0] == 3
    rep = report(db, pid, labels, tmp_path)
    assert rep["items"][0]["decision"] | {"at": ""} == {
        "verdict": "overridden", "reason": "prescriber consulted", "by": "amy", "at": ""}
    assert "[overridden by amy]" in "\n".join(review.report_lines(rep))
    # a decision is about what the item said: a changed result is undecided again
    with closing(patients.connect(db)) as conn:
        changed = review.cached(conn, ALPHA, BETA, labels, "standin") | {"status": C}
        patients.pair_put(conn, *review._key(ALPHA, BETA, labels, "standin"), changed)
    again = next(i for i in report(db, pid, labels, tmp_path)["items"]
                 if i["title"] == "alphazine hydrochloride + betamol")
    assert again["status"] == C and again["decision"] is None and again["key"] != first


# ── through the API ──────────────────────────────────────────────────────────

def tthereportanddecisionroutes(clinic, labels, tmp_path):
    from pharmacli import serve

    serve.app.dependency_overrides[serve.get_labels] = lambda: labels
    clinic.headers.update(signin(clinic))
    csv = "name,dob,drugs,egfr\nAda Example,1961-04-09,alphazine; betamol; unobtainium,38\n"
    assert clinic.post("/patients/upload", content=csv.encode()).json()["created"] == 1
    pid = clinic.get("/patients").json()["patients"][0]["id"]
    rep = clinic.get(f"/patients/{pid}/report").json()
    assert rep["summary"].startswith("0 of 3 pairs checked; pending: alphazine hydrochloride")

    # the model's name comes from the settings the server runs with
    from pharmacli.config import load
    userag(lambda r: review.work(r, clinic.db, labels), FakeChroma(), corpus=PATIENT_CORPUS,
           reply=model(ANSWERS), llm=load().llm)
    rep = clinic.get(f"/patients/{pid}/report").json()
    assert rep["checked"] == 1 and rep["items"][0]["status"] == A
    item = rep["items"][0]["key"]

    done = clinic.post(f"/patients/{pid}/decisions",
                       json={"item": item, "verdict": "overridden", "reason": " seen by MD "})
    assert done.json()["items"][0]["decision"]["reason"] == "seen by MD"
    assert done.json()["items"][0]["decision"]["by"] == "rph"
    listing = clinic.get("/patients").json()
    assert listing["decisions"] == {"accepted": 0, "overridden": 1}       # shown to pharmacists
    assert clinic.get("/metrics").json()["decisions"] == {"accepted": 0, "overridden": 1}

    bad = clinic.post(f"/patients/{pid}/decisions", json={"item": "0" * 16, "verdict": "accepted"})
    assert bad.status_code == 404 and "not on this patient's report" in bad.json()["detail"]
    assert clinic.post(f"/patients/{pid}/decisions",
                       json={"item": item, "verdict": "maybe"}).status_code == 422
    assert clinic.get(f"/patients/{'0' * 16}/report").status_code == 404
    with closing(patients.connect(clinic.db)) as conn:
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit ORDER BY id")]
    assert "GET /patients/{pid}/report" in actions and "POST /patients/{pid}/decisions" in actions


# ── scoring the pair answer key ──────────────────────────────────────────────

def tthepairkeyisscoredonagreementseveremissesandmanualreview(labels):
    rows = [{"id": "p1", "a": "alphazine", "b": "betamol", "expect": A},
            {"id": "p2", "a": "alphazine", "b": "gammavir", "expect": A},      # the model says B
            {"id": "p3", "a": "betamol", "b": "gammavir", "expect": NONE},
            {"id": "p4", "a": "betamol", "b": "deltaquin", "expect": C},       # ends in review
            {"id": "old", "a": "alphazine", "b": "deltaquin", "expect": C,
             "label_versions": {"alpha": 1}},                                  # the label moved on
            {"id": "gone", "a": "alphazine", "b": "warfarin", "expect": C}]
    result = userag(lambda r: rageval.run_pairs(r, rows, labels), FakeChroma(),
                    corpus=PATIENT_CORPUS, reply=model(ANSWERS), llm="standin")
    assert result["summary"] == {"pairs": 4, "stale": 2, "agree": 2, "stated_pairs": 3,
                                 "stated_agree": 1, "severe_pairs": 2,
                                 "severe_placed_milder": 1, "manual_review": 1}
    got = {r["id"]: r.get("got") for r in result["rows"]}
    assert got == {"p1": A, "p2": B, "p3": NONE, "p4": review.MANUAL, "old": None, "gone": None}
    assert json.dumps(result)                                # the --json dump must serialise
    # manual review is not "milder": only a lower category counts as a miss
    assert rageval.pair_summary([{"id": "x", "expect": A, "got": review.MANUAL}])[
        "severe_placed_milder"] == 0


def taninteractiontableisquotedfromitsinterventiontoitsexamples(labels, tmp_path):
    # the layout labels use: Clinical Impact, Intervention, Examples -- the
    # drug is named in the last row and what to do is in the one before
    label(tmp_path, "alpha", inter=[
        "Testase Inducers", "Clinical Impact: Levels of alphazine fall.",
        "Intervention: If use together is necessary, increase the alphazine dosage.",
        "Examples: Rifampin, betamol, phenytoin Sedatives Clinical Impact: Sedation adds up.",
        "Intervention: Limit the dose.", "Examples: Diazepam."])
    name, kind = review.terms(BETA, CLASSES)
    own = review.terms(ALPHA, CLASSES)[0]
    found, _ = review.mentions(ALPHA.labels[0], own, name, kind, str(tmp_path))
    assert [s["text"] for s in found] == [
        "Intervention: If use together is necessary, increase the alphazine dosage. "
        "Examples: Rifampin, betamol, phenytoin"]      # not the next row's title or its impact
    assert review.worded(found[0]["text"]) == 1                  # reads as "adjust the dose"

    # The PDF runs the next row's title on from the last example. A class named
    # in that title belongs to the row it heads: quoting the Intervention above
    # it would pin the sedatives' instruction on the inducers.
    label(tmp_path, "alpha", inter=[
        "Sedatives", "Clinical Impact: Sedation adds up.",
        "Intervention: Halve the sedative dose.",
        "Examples: Diazepam, lorazepam CYP3A4 Inducers Clinical Impact: Alphazine levels fall.",
        "Intervention: Monitor the response.", "Examples: Rifampin, phenytoin."])
    review._label.cache_clear()
    found, _ = review.mentions(ALPHA.labels[0], own, name, kind, str(tmp_path))
    assert [s["text"] for s in found] == [
        "CYP3A4 Inducers Clinical Impact: Alphazine levels fall. "
        "Intervention: Monitor the response."]
    assert review.worded(found[0]["text"]) == 2 and "Halve" not in found[0]["text"]
    assert review._next_row("Examples: Rifampin, phenytoin Benzodiazepines and Others ") == 30
    assert review._next_row("Examples: other opioids, alcohol.") == -1
    # a cross-reference and a ruled-out adjustment are not wording about the pair
    assert review.worded("May raise levels [see Contraindications (4)].") == 2
    assert review.worded("Exposure falls. No dose adjustment is warranted.") == 2


# ── the committed reference files ────────────────────────────────────────────

def real(name: str):
    path = ROOT / name
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] \
        if name.endswith(".jsonl") else json.loads(path.read_text(encoding="utf-8"))


def ttheclasstermsfilecoverseverydrugandsaysitisunreviewed():
    classes, known = real("classes.json"), review.drugs(real("corpus.json"))
    assert classes["reviewed"] is False and "PROVISIONAL" in classes["note"]
    assert set(classes["drugs"]) == set(known)
    for key, entry in classes["drugs"].items():
        # a class from openFDA, or a hand-written term -- and every such term has its source
        assert entry["pharm_class"] or entry.get("terms"), key
        assert all(t["term"] and "label" in t["source"] for t in entry.get("terms", [])), key
        assert review.terms(known[key], classes)[1] is not None, key
    # every alias hangs off a class openFDA actually returned for some drug
    returned = {c for entry in classes["drugs"].values() for c in entry["pharm_class"]}
    assert set(classes["aliases"]) <= returned


def tthepairkeytieseveryexpectedcategorytoapassage():
    rows, corpus = real("tests/pairs_qa.jsonl"), real("corpus.json")
    known = review.drugs(corpus)
    assert len(rows) >= 15 and len({r["id"] for r in rows}) == len(rows)
    wanted = {tuple(sorted(p)) for p in patients.KEY_PAIRS}
    assert wanted <= {tuple(sorted((r["a"], r["b"]))) for r in rows}     # the five it starts from
    assert {r["expect"] for r in rows} == set(review.CATEGORIES)         # every category appears
    for r in rows:
        assert r["a"] in known and r["b"] in known and r["reviewed"] is False, r["id"]
        mine = {e["doc_id"] for k in (r["a"], r["b"]) for e in known[k].labels}
        assert set(r["label_versions"]) == mine, r["id"]
        if r["expect"] == NONE:
            # backed by the record of what was searched, in every label of both drugs
            assert r["passage"] is None and len(r["searched"]) == len(mine), r["id"]
        else:
            assert r["passage"]["doc_id"] in mine and r["passage"]["page"] >= 1, r["id"]
            assert len(r["passage"]["quote"]) > 20, r["id"]


def tthepairkeysquotesareinthelabelswhenthepdfsarehere():
    from pharmacli import docs, rag

    rows = real("tests/pairs_qa.jsonl")
    folder = ROOT / "data" / "docs"
    if not all((folder / f"{d}.pdf").exists() for r in rows for d in r["label_versions"]):
        pytest.skip("the label PDFs are not on this machine -- pharmacli docs fetch")
    versions = {e["doc_id"]: e["version"] for e in real("corpus.json")["inserts"]}
    for r in rows:
        if r["passage"] is None or versions[r["passage"]["doc_id"]] != \
                r["label_versions"][r["passage"]["doc_id"]]:
            continue                                  # nothing to find, or the label moved on
        doc = r["passage"]["doc_id"]
        pages = [rag.page_at(starts, text.find(r["passage"]["quote"]))
                 for _, _, lines in review._label(doc, versions[doc], str(folder))
                 for text, starts in [rag.join(lines)] if r["passage"]["quote"] in text]
        assert pages == [r["passage"]["page"]], r["id"]
    assert docs.DOCS_DIR == "data/docs"


def tclassesarelookeduponceandamissiskepttoo():
    from pharmacli import docs

    asked = []

    def lookup(ndc):
        asked.append(ndc)
        return {"pharm_class": ["Zeta Blocker [EPC]"]} if ndc == "22222222201" else None

    docs.PAUSE, pause = 0, docs.PAUSE
    try:
        known = {"reviewed": True, "aliases": {"X [EPC]": ["x"]},
                 "drugs": {"alphazine": {"pharm_class": ["Kept [EPC]"], "terms": [{"term": "t"}]}}}
        got = docs.classes(PATIENT_CORPUS, known, lookup)
        assert got["drugs"]["alphazine"] == known["drugs"]["alphazine"]      # not asked again
        assert got["drugs"]["betamol"]["pharm_class"] == ["Zeta Blocker [EPC]"]
        assert got["drugs"]["gammavir"]["pharm_class"] == [] and got["drugs"]["gammavir"]["looked"]
        assert "11111111101" not in asked and asked.count("22222222201") == 1
        assert got["reviewed"] is True and got["aliases"] == known["aliases"]   # carried over
        again = len(asked)
        docs.classes(PATIENT_CORPUS, got, lookup)
        assert len(asked) == again                                           # a miss is kept too
    finally:
        docs.PAUSE = pause
    assert docs.load_classes("no-such-file.json") == {"aliases": {}, "drugs": {}}
