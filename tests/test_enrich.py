import pytest

enrich = pytest.importorskip("pharmacli.enrich")


def tpadnormalisesopenfdatoeleven():
    # openFDA keeps the original segmentation; NADAC pads every segment
    assert enrich.pad("0378-3545-01") == "00378354501"   # 4-4-2 labeler
    assert enrich.pad("00378-3545-01") == "00378354501"  # already 5-4-2
    assert enrich.pad("12345-678-9") == "12345067809"    # 5-3-1
    assert len(enrich.pad("0378-3545-01")) == 11


def tpadrejectsmalformed():
    assert enrich.pad("0378-3545") is None    # 2 segments, not a package code
    assert enrich.pad("003783545011") is None  # no hyphens at all


def tallthreesegmentationsaretried():

    cand = enrich._candidates          # was a _cand() wrapper defined below
    # labeler 0378 is 4 digits -> both the stored 5-4 and the 4-4-2 form
    assert cand("00378351501") == ["00378-3515", "0378-3515"]
    # product 678 is 3 digits -> the 5-3-2 form, the branch that was missing
    assert cand("12345067890") == ["12345-0678", "12345-678"]
    # neither segment padded -> only the stored form is possible
    assert cand("12345678901") == ["12345-6789"]
    # both padded -> all three
    assert len(cand("01234056789")) == 3


# ── price history ────────────────────────────────────────────────────────────
# `lookup --history` is a flag rather than a command because this is
# enrich.local's query with LIMIT 1 removed. Tested at the pure-function level:
# cmdlookup itself calls openFDA, and a test must not go to the network.
def thistoryreturnseveryweekoldestfirst(tmp_path):
    from conftest import row
    from pharmacli.models.medicine import Medicine
    from pharmacli.store import connect, insert

    db = str(tmp_path / "h.db")
    conn = connect(db)
    weeks = ["2026-01-21", "2026-01-07", "2026-01-14"]     # inserted out of order
    insert(conn, [m for m in
                  (Medicine.arow(row("00000000001", f"{1.0 + i:.5f}", w), i)
                   for i, w in enumerate(weeks))
                  if isinstance(m, Medicine)])
    conn.close()

    got = enrich.history("00000000001", db)
    assert [d for d, _ in got] == sorted(weeks), "must be oldest first"
    assert got[0][1] == 2.0        # the 2026-01-07 row, whatever order it went in


def thistorypadsthendclikeeverythingelse(tmp_path):
    from conftest import row
    from pharmacli.models.medicine import Medicine
    from pharmacli.store import connect, insert

    db = str(tmp_path / "h.db")
    conn = connect(db)
    insert(conn, [m for m in [Medicine.arow(row("00000000001", "1.00000", "2026-01-07"), 1)]
                  if isinstance(m, Medicine)])
    conn.close()
    # an unpadded NDC is what a pharmacist types off a label
    assert enrich.history("1", db) == enrich.history("00000000001", db)


def tsparklinescalesandneverliesaboutflat():
    from pharmacli.cli import BLOCKS, _sparkline

    rising = _sparkline([1.0, 2.0, 3.0, 4.0])
    assert rising[0] == BLOCKS[0] and rising[-1] == BLOCKS[-1]
    # a flat series at the BOTTOM of the range would read as a collapse to zero;
    # it has not moved, so it sits in the middle
    flat = _sparkline([5.0] * 6)
    assert set(flat) == {BLOCKS[len(BLOCKS) // 2]}
    assert len(_sparkline([1.0, 2.0, 3.0])) == 3


# ── pack sizes ───────────────────────────────────────────────────────────────
def tpacksizereadstheleadingcount():
    assert enrich.packsize("30 TABLET, FILM COATED in 1 BOTTLE (82804-054-30)") == 30
    assert enrich.packsize("1,000 TABLET in 1 BOTTLE") == 1000     # comma survives
    assert enrich.packsize("500 mL in 1 BOTTLE") == 500


def tpacksizerefusestoguess():
    for bad in ("BOTTLE of stuff", "", "   ", "0 TABLET in 1 BOTTLE", "-5 mL"):
        assert enrich.packsize(bad) is None


def tpackcacherecordsmissestoo(tmp_path):
    from pharmacli.store import connect, packages, save_package

    conn = connect(str(tmp_path / "p.db"))
    try:
        save_package(conn, "00000000001", 500.0, "500 TABLET in 1 BOTTLE")
        save_package(conn, "00000000002", None, "")          # a cached miss
        got = packages(conn)
        assert got["00000000001"][0] == 500.0
        assert "00000000002" in got and got["00000000002"][0] is None
    finally:
        conn.close()
