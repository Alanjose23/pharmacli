import argparse

import pytest
from conftest import row

from pharmacli import ask
from pharmacli.cli import EXIT_OK, cmdtoday
from pharmacli.desk import format_worklists, worklists
from pharmacli.models.medicine import Medicine
from pharmacli.store import connect, insert, watch_cost

WEEK = "2026-01-07"
SINKING = "00000000001"      # costs more than NADAC -- shows up under APPEAL
FINE = "00000000002"


def shelf(tmp_path):
    db = str(tmp_path / "a.db")
    conn = connect(db)
    meds = [Medicine.arow(row(SINKING, "1.00000", WEEK), 1),
            Medicine.arow(row(FINE, "5.00000", WEEK), 2)]
    insert(conn, [m for m in meds if isinstance(m, Medicine)])
    watch_cost(conn, [(SINKING, 1.50, 100.0, 200.0),
                      (FINE, 1.00, 100.0, 200.0)])
    conn.close()
    return db


def tsameformatter(tmp_path, capsys):
    db = shelf(tmp_path)
    assert cmdtoday(argparse.Namespace(db=db, within=21, dead_days=180)) == EXIT_OK
    printed = capsys.readouterr().out.rstrip("\n")

    w = worklists(db, 21, 180)
    assert "\n".join(format_worklists(w, 21, 180)) == printed

    # and the question answers out of that same dict, header included
    section = ask.underwater(db=db)
    assert section[1] in printed and section[-1] in printed


def tworklistsreportsmissingdatainsteadofraising(tmp_path):
    db = str(tmp_path / "empty.db")
    connect(db).close()
    w = worklists(db)
    assert "error" in w and "pharmacli fetch" in w["error"]
    assert ask.underwater(db=db) == [w["error"]]


def teverylabelmatchesitself():
    for label, _, _ in ask.QUESTIONS:
        score, hit, _, _ = ask.match(label)[0]
        assert hit == label, f"{label!r} matched {hit!r}"
        assert score >= ask.CUTOFF, f"{label!r} scored {score:.2f}"


def tnolabelcollides():
    for label, _, _ in ask.QUESTIONS:
        ranked = ask.match(label)
        assert ranked[0][0] > ranked[1][0], f"{label!r} ties with {ranked[1][1]!r}"


def tneverguesses(monkeypatch):
    # with the documents lane unavailable, a clinical question still gets the
    # menu, never a price answer
    monkeypatch.setattr(ask, "_documents", lambda q: None)
    for q in ("what dose of lisinopril should I give",
              "is this safe with warfarin",
              "what is the maximum daily dose"):
        top = ask.match(q)[0]
        assert top[0] < ask.CUTOFF, f"{q!r} matched {top[1]!r} at {top[0]:.2f}"
        assert "no question matches" in ask.answer(q)[1][0]


def tdocumentquestionsleavethepricelane():
    documents = ("what are the contraindications for this drug",
                 "what drug interactions does warfarin have",
                 "what is the boxed warning for this drug",
                 "can this drug be crushed",
                 "how does CMS calculate NADAC",
                 # a reimbursement question full of price vocabulary: the shelf
                 # questions must not claim it (biz-04 in the answer key)
                 "how many pharmacies have to report a price before the NADAC "
                 "help desk will review it",
                 "what is the deadline for a MAC appeal in New York",
                 "what is the capital of France",
                 "how should I store this drug",
                 "does medicaid cover this drug")
    price = [label for label, _, _ in ask.QUESTIONS] + [
        "which drugs am I losing money on", "what is this drug 59148001013",
        "what am I losing money on right now",
        "which drugs in my store are losing money",
        "what should I order for the store this week",
        "when do medicaid prices change next",
        "what am I losing money on with medicaid"]
    for q in documents:
        assert ask.lane(q) == "documents", q
    for q in price:
        assert ask.lane(q) == "price", q


def tdocumentslanehandsofftorag(monkeypatch):
    monkeypatch.setattr(ask, "_documents", lambda q: ["Avoid below eGFR 30 [1].", "", "[1] X"])
    assert ask.answer("what are the contraindications for this drug") == (
        "documents", ["Avoid below eGFR 30 [1].", "", "[1] X"], [])


def tdocumentslaneoutagesaysso(monkeypatch):
    # a doc-word question that ALSO scores highly against a price label must
    # not silently fall back to a plain "no question matches" -- the outage
    # has to say so, not look like an unrecognized question
    monkeypatch.setattr(ask, "_documents", lambda q: None)
    label, lines, _ = ask.answer("what is this drug dosage")
    assert label == ""
    assert lines[0].startswith("no question matches")
    assert lines[1] == ask.DOCUMENTS_DOWN


def tpartialquestionsstillmatch():
    for short, expect in (("losing money", "what am I losing money on"),
                          ("order this week", "what should I order this week"),
                          ("capital sitting dead", "what capital is sitting dead")):
        assert ask.match(short)[0][1] == expect


def targscomeoutofthesentence():
    from pharmacli.ask import args_from

    a = args_from("what is this drug 59148001013", "what is this drug")
    assert a["ndc"] == "59148001013"

    # hyphenated and spaced NDCs are the same NDC
    assert args_from("drug 5914-8001-013", "")["ndc"] == "59148001013"

    # the label's words are stripped so the phrase is only the useful part
    a = args_from("find a drug by description cholesterol medication",
                   "find a drug by description")
    assert a["phrase"] == "cholesterol medication"

    # no digits, no ndc key -- answer() then reports what it needs
    assert "ndc" not in args_from("what is on my shelf", "what is on my shelf")


def tcheckscopestothefile(tmp_path):
    from pharmacli.cli import read_inventory
    from pharmacli.desk import worklists
    from pharmacli.store import costs

    db = shelf(tmp_path)                       # SINKING + FINE are on the watch list
    csv = tmp_path / "vendor.csv"
    csv.write_text(f"ndc,cost\n{SINKING},9.00\n99999999999,1.00\nbad,1\n")

    rows, skipped = read_inventory(csv.as_posix())
    assert [r[0] for r in rows] == [SINKING, "99999999999"]
    assert len(skipped) == 2                   # the header and the bad row

    mine = {n: c for n, c, _, _ in rows}
    w = worklists(db, shelf=(mine, {n: (None, None) for n in mine}))
    # scoped: FINE is on the watch list but NOT in the file, so it is absent
    assert w["priced"] == 2
    assert [u[1] for u in w["under"]] == [SINKING]
    assert w["unpriced"] == ["99999999999"]    # no NADAC row for a made-up NDC

    # and reading a file must not adopt it
    conn = connect(db)
    try:
        assert "99999999999" not in costs(conn)
    finally:
        conn.close()


def twritedrugsonerowperdrug(tmp_path):
    # one row per shelf drug, carrying the numbers the chat itself prints
    import csv

    db = shelf(tmp_path)
    out = tmp_path / "drugs.csv"
    assert ask.write_drugs(out.as_posix(), db=db) == 2

    with open(out, newline="", encoding="utf-8") as fh:
        rows = {r["ndc"]: r for r in csv.DictReader(fh)}
    assert list(rows) == [SINKING, FINE]
    assert list(next(iter(rows.values()))) == ask.DRUG_COLUMNS

    r = rows[SINKING]
    assert float(r["your_cost"]) == 1.5 and float(r["nadac_unit"]) == 1.0
    # the same breakeven() the CLI prints, not a second calculation of it
    from pharmacli.config import load
    from pharmacli.desk import breakeven
    assert r["breakeven"] == breakeven(1.5, 1.0, load().dispensing_fee, "EA")


def tnewquestionsrouteandsurviveoffline(monkeypatch):
    import httpx

    from pharmacli import safety

    assert ask.match("which drugs are recalled")[0][1] == "which stocked drugs are recalled"
    assert (ask.match("which brands have a cheaper generic")[0][1]
            == "which brands have cheaper generics")
    assert ask.match("what is on my shelf")[0][1] == "what is on my shelf"
    assert ask.match("what am I losing money on")[0][1] == "what am I losing money on"

    def offline(db=None, shelf=None):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(safety, "shelf_recalls", offline)
    assert ask.stocked_recalls()[0].startswith("openFDA recall search failed")


def tfinddrugworkswithoutthenlpmodule(tmp_path, monkeypatch):
    import sys

    # a core install: nlp.py needs numpy and sklearn, so it cannot import
    monkeypatch.setitem(sys.modules, "pharmacli.nlp", None)
    out = ask.find_drug(shelf(tmp_path), f"drug {SINKING}")
    assert out[0].startswith("keyword matches")
    assert f"  DRUG {SINKING}   {SINKING}" in out


def tanswerusesthepassedshelfinsteadofthedatabase(tmp_path):
    db = shelf(tmp_path)
    # the saved shelf has SINKING at 1.50 against a NADAC price of 1.00
    _, saved, _ = ask.answer("what am I losing money on", db=db)
    assert any("1 positions cost more than NADAC" in line for line in saved)

    # an uploaded file carrying only FINE, priced below NADAC, is clean
    upload = ({FINE: 1.00}, {FINE: (100.0, 200.0)})
    _, uploaded, _ = ask.answer("what am I losing money on", db=db, shelf=upload)
    assert any("nothing underwater" in line for line in uploaded)


def tshelfsummarycountsthepassedshelf(tmp_path):
    db = shelf(tmp_path)
    upload = ({FINE: 1.00}, {FINE: (100.0, 200.0)})
    assert ask.shelf_summary(db=db, shelf=upload)[0] == "1 positions with a recorded cost"


def tunpricedlistsndcswithnopriceinthenewestfile(tmp_path):
    db = shelf(tmp_path)
    upload = ({SINKING: 1.50, "00000000099": 2.00},
              {SINKING: (100.0, 200.0), "00000000099": (10.0, 5.0)})
    out = ask.unpriced(db=db, shelf=upload)
    assert "1 of 2 NDCs have no price" in out[0]
    assert "  00000000099" in out


def tshelfchangescomparesanuploadwiththesavedshelf(tmp_path):
    db = shelf(tmp_path)          # saved: SINKING at 1.50, FINE at 1.00
    upload = ({SINKING: 1.75, "00000000099": 2.00},
              {SINKING: (100.0, 200.0), "00000000099": (10.0, 5.0)})
    out = "\n".join(ask.shelf_changes(db=db, shelf=upload))
    assert "1 NDC in the file is not on your saved shelf" in out
    assert "00000000099" in out                      # added
    assert "1 NDC on your saved shelf is not in the file" in out
    assert FINE in out                               # dropped
    assert "+0.25000" in out                         # SINKING cost moved 1.50 -> 1.75


def tshelfchangesneedsafile(tmp_path):
    out = ask.shelf_changes(db=shelf(tmp_path))
    assert out == ["attach an inventory CSV first, or run: pharmacli check FILE.csv"]


def tsuggestionsskipemptylistsandnamerealquestions(tmp_path):
    db = shelf(tmp_path)
    labels = {label for label, _, _ in ask.QUESTIONS}

    # an upload holding only FINE: priced below NADAC, so nothing to appeal
    got = ask.suggestions(db=db, shelf=({FINE: 1.00}, {FINE: (100.0, 200.0)}))
    assert all(s["q"] in labels for s in got)
    assert len(got) <= 4
    assert not any(s["q"] == "what am I losing money on" for s in got)

    # an upload holding SINKING: costs more than NADAC, so the appeal leads
    got = ask.suggestions(db=db, shelf=({SINKING: 1.50}, {SINKING: (100.0, 200.0)}))
    assert got[0]["q"] == "what am I losing money on"
    assert "$" in got[0]["hint"]


# ── drugs by name, and what to ask next ──────────────────────────────────────
LIPITOR10, LIPITOR20 = "00071015523", "00071015623"
ATOR_QUIET, ATOR_BUSY, ATOR10 = "00093505601", "00093505602", "00093505501"


def named(tmp_path, drugs, stocked=()):
    db = str(tmp_path / "n.db")
    conn = connect(db)
    insert(conn, [Medicine.arow(row(n, "1.00000", WEEK) | {"NDC Description": d}, i)
                  for i, (n, d) in enumerate(drugs.items(), 1)])
    watch_cost(conn, [(n, 1.0, 100.0, monthly) for n, monthly in stocked])
    conn.close()
    return db


def tadrugquestionneverreadsthekeyboard(monkeypatch):
    import builtins
    import io
    import sys

    # `pharmacli chat` runs the server in a terminal: an input() there hangs
    # the browser on "..." until someone types into the server's shell
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr(builtins, "input", lambda *a: pytest.fail("read the keyboard"))
    label, lines, _ = ask.answer("what is this drug")
    assert label == "what is this drug"
    assert lines[0].startswith("which drug?")


def tadrugnameresolvestoitsndc(tmp_path):
    db = named(tmp_path, {LIPITOR10: "LIPITOR 10 MG TABLET",
                          LIPITOR20: "LIPITOR 20 MG TABLET"})
    label, lines, _ = ask.answer("what is this drug lipitor 20", db=db)
    text = "\n".join(lines)
    assert label == "what is this drug"
    assert LIPITOR20 in text and LIPITOR10 not in text


def tanamethatfitsseveraldrugsoffersthemall(tmp_path):
    db = named(tmp_path, {LIPITOR10: "LIPITOR 10 MG TABLET",
                          LIPITOR20: "LIPITOR 20 MG TABLET"})
    label, lines, chips = ask.answer("what has lipitor's price done", db=db)
    assert label == "what has this drug's price done"
    assert "which one" in lines[0]
    assert {c["q"] for c in chips} == {f"what has this drug's price done {LIPITOR10}",
                                       f"what has this drug's price done {LIPITOR20}"}


def tanunknownnameisnotinthepanel(tmp_path):
    db = named(tmp_path, {LIPITOR10: "LIPITOR 10 MG TABLET"})
    _, lines, _ = ask.answer("what is this drug zocor", db=db)
    assert "not in the panel" in lines[0]


def tanamepicksthebusiestndconyourshelf(tmp_path):
    # the 10 MG is not stocked, so it is not a choice; of the two 20 MG
    # positions, the one that moves 50 a month is the one you mean
    db = named(tmp_path, {ATOR_QUIET: "ATORVASTATIN 20 MG TABLET",
                          ATOR_BUSY: "ATORVASTATIN 20 MG TABLET",
                          ATOR10: "ATORVASTATIN 10 MG TABLET"},
               stocked=[(ATOR_QUIET, 10.0), (ATOR_BUSY, 50.0)])
    _, lines, _ = ask.answer("when should I reorder atorvastatin", db=db)
    text = "\n".join(lines)
    assert ATOR_BUSY in text and ATOR_QUIET not in text and ATOR10 not in text


def tadruganswerofferstheotherdrugquestions(tmp_path):
    db = named(tmp_path, {LIPITOR10: "LIPITOR 10 MG TABLET"})
    _, _, chips = ask.answer(f"what is this drug {LIPITOR10}", db=db)
    assert {c["q"] for c in chips} == {f"what has this drug's price done {LIPITOR10}",
                                       f"will this drug's price move {LIPITOR10}",
                                       f"when should I reorder this drug {LIPITOR10}"}


def tmostsharedwordsbeatashortlabel():
    # "what is this drug" scores 1.0 on "drug" alone; "find" is the word that matters
    assert ask.match("find a drug cholesterol pill")[0][1] == "find a drug by description"


def tnomatchoffersnearquestionsthatwaitforadrug(monkeypatch):
    monkeypatch.setattr(ask, "_documents", lambda q: None)
    label, lines, chips = ask.answer("price history for atorvastatin")
    assert label == ""
    assert {"q": "what has this drug's price done", "fill": True} in chips
    assert not any("pharmacli ask" in line for line in lines)   # the CLI menu is the CLI's


def tclinomatchstillprintsthemenu(monkeypatch, capsys):
    from pharmacli.cli import cmdask

    monkeypatch.setattr(ask, "_documents", lambda q: None)
    cmdask(argparse.Namespace(out=None, question="price history for atorvastatin",
                              db=None, ndc=None, phrase=None))
    out = capsys.readouterr().out
    assert "did you mean" in out and "what has this drug's price done" in out
    assert "what am I losing money on" in out
