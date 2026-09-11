import argparse


from conftest import row
from pharmacli import ask
from pharmacli.cli import EXIT_OK, cmdtoday, format_worklists, worklists
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


def tneverguesses():
    for q in ("what dose of lisinopril should I give",
              "is this safe with warfarin",
              "what is the maximum daily dose"):
        top = ask.match(q)[0]
        assert top[0] < ask.CUTOFF, f"{q!r} matched {top[1]!r} at {top[0]:.2f}"
        assert "no question matches" in ask.answer(q)[1][0]


def tpartialquestionsstillmatch():
    for short, expect in (("losing money", "what am I losing money on"),
                          ("order this week", "what should I order this week"),
                          ("capital sitting dead", "what capital is sitting dead")):
        assert ask.match(short)[0][1] == expect


def texplainquotesverbatim():
    out = ask.explain(topic="MATERIAL_SWING")
    assert out[0].startswith("cli.py:")            # provenance first
    body = "\n".join(out)
    # text that exists in cli.py and nowhere else, reproduced exactly
    assert "median swing $0.09, p95 $0.69" in body
    assert "The dispensing fee ($10.64) was the obvious candidate" in body


def texplainadmitsignorance():
    out = ask.explain(topic="quantum chromodynamics")
    assert "nothing in the source explains" in out[0]


def tchunksarescopedtoblocks():
    ch = ask.chunks()
    assert 40 < len(ch) < 400
    # the MATERIAL_SWING measurement must sit in ONE chunk with its conclusion,
    # not be split from it -- that is what by-function chunking got wrong
    hit = [t for _, _, t in ch if "median swing $0.09" in t]
    assert len(hit) == 1 and "per-drug timing is worth CENTS" in hit[0]
    assert not any(f == "ask.py" for f, _, _ in ch)     # no self-reference


def targscomeoutofthesentence():
    from pharmacli.chat import _args_from

    a = _args_from("what is this drug 59148001013", "what is this drug")
    assert a["ndc"] == "59148001013"

    # hyphenated and spaced NDCs are the same NDC
    assert _args_from("drug 5914-8001-013", "")["ndc"] == "59148001013"

    # the label's words are stripped so the phrase is only the useful part
    a = _args_from("find a drug by description cholesterol medication",
                   "find a drug by description")
    assert a["phrase"] == "cholesterol medication"

    # no digits, no ndc key -- answer() then reports what it needs
    assert "ndc" not in _args_from("what is on my shelf", "what is on my shelf")


def tcheckscopestothefile(tmp_path):
    from pharmacli.cli import read_inventory, worklists
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
    # break-even is the chat's own string, not a second calculation of it
    assert r["breakeven"] and r["breakeven"] in "\n".join(ask.breakeven_qty(db=db, ndc=SINKING))
