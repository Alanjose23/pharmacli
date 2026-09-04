

from datetime import date
from decimal import Decimal

from pharmacli.models.medicine import Medicine, parsedate, parseotc, parseprice
from pharmacli.models.rows import COLUMNS, RawRow, SkippedRow
from pharmacli.store import connect, count, insert

GOOD = "STRIBILD TABLET,61958120101,150.40902,01/01/2026,EA,C/I,N,4,B,0.23432,05/20/2026,04/29/2026"


def row(line: str) -> RawRow:
    return dict(zip(COLUMNS, line.split(","))) 

 # each rejection has its own reason
def tparseprice():
    assert parseprice("150.40902").value == Decimal("150.40902")
   
    assert parseprice("").reason == "missing price"
    assert parseprice("abc").reason == "malformed price"
    assert parseprice("NaN").reason == "malformed price"
    assert parseprice("-1").reason == "non-positive price"
    assert parseprice("0").reason == "non-positive price"


def tparsedate():
    assert parsedate("01/02/2026") == date(2026, 1, 2)
    assert parsedate("2026-01-02") == date(2026, 1, 2)
    assert parsedate("01/02/26") == date(2026, 1, 2)
    assert parsedate("") is None
    assert parsedate("not a date") is None


def tparseotc():
    assert parseotc("Y") is True
    assert parseotc("n") is False
    assert parseotc("maybe") is None


def tarowaccrej():
    med = Medicine.arow(row(GOOD), 2)
    assert isinstance(med, Medicine)
    assert med.drug.ndc == "61958120101"
    assert med.rateclass == "B"
    assert med.generic is not None

    for column, reason in (("NDC", "Missing NDC"), ("NDC Description", "Missing Description")):
        broken = row(GOOD) | {column: ""}
        assert Medicine.arow(broken, 2) == SkippedRow(2, reason)


def tmissgenpprice():
    med = Medicine.arow(row(GOOD) | {"Corresponding Generic Drug NADAC Per Unit": ""}, 2)
    assert isinstance(med, Medicine)
    assert med.generic is None


def troundtrips():
    med = Medicine.arow(row(GOOD), 2)
    assert isinstance(med, Medicine)
    again = Medicine.arow(dict(zip(COLUMNS, med.torow())), 2)
    assert again == med


def tstoreround():
    med = Medicine.arow(row(GOOD), 2)
    assert isinstance(med, Medicine)
    conn = connect(":memory:")

    assert insert(conn, [med]) == 1
    # interrupted fetch resume
    assert insert(conn, [med]) == 0
    assert count(conn) == 1

    stored = conn.execute("SELECT ndc, unit_price, effective_date, otc FROM nadac").fetchone()
    assert stored == ("61958120101", "150.40902", "2026-01-01", "N")

    # the five columns the store used to drop
    # pharmacy_type dropped from this list: it is no longer stored, because it
    # held "C/I" on every one of 1,028,250 fetched rows. Medicine still parses
    # it -- torow() has to emit all 12 CSV columns -- it just is not persisted.
    wide = conn.execute(
        "SELECT rate_class, generic_price, generic_effective_date,"
        " explanation_code FROM nadac"
    ).fetchone()
    assert wide == ("B", "0.23432", "2026-05-20", "4")

    # no generic equivalent empties both generic columns
    plain = Medicine.arow(
        row(GOOD) | {"NDC": "00000000001", "Corresponding Generic Drug NADAC Per Unit": ""},
        3,
    )
    assert isinstance(plain, Medicine)
    assert insert(conn, [plain]) == 1
    assert conn.execute(
        "SELECT generic_price, generic_effective_date FROM nadac WHERE ndc = '00000000001'"
    ).fetchone() == ("", "")
    conn.close()


def twatchroundtrip(tmp_path):
    """The stock list. NDCs normalise to 11 digits on the way in, so the same
    drug entered two ways is one entry, not two."""
    from pharmacli.store import watch_add, watch_remove, watched

    conn = connect(str(tmp_path / "w.db"))
    assert watch_add(conn, ["378351501", "00093721410"]) == 2
    assert watched(conn) == ["00093721410", "00378351501"]   # zero-padded, sorted

    # re-adding the same drug unpadded must be a no-op, not a duplicate
    assert watch_add(conn, ["00378351501", "378351501"]) == 0
    assert len(watched(conn)) == 2

    assert watch_remove(conn, ["93721410"]) == 1             # unpadded removal works
    assert watched(conn) == ["00378351501"]
    conn.close()


def twatchsurvivesreconnect(tmp_path):
 
    from pharmacli.store import watch_add, watched

    path = str(tmp_path / "w.db")
    conn = connect(path); watch_add(conn, ["00378351501"]); conn.close()
    again = connect(path)
    assert watched(again) == ["00378351501"]
    again.close()


def tcostsurvivemigrationandupsert(tmp_path):

    from pharmacli.store import costs, watch_add, watch_cost, watched

    path = str(tmp_path / "m.db")
    conn = connect(path)
    watch_add(conn, ["00378351501"])           # on watch, no cost yet
    assert costs(conn) == {}
    conn.close()

    again = connect(path)                       # re-open: ALTER must not wipe it
    assert watched(again) == ["00378351501"]

    # importing an invoice twice is an update, not a duplicate row
    watch_cost(again, [("378351501", 0.091, None, None)])
    watch_cost(again, [("00378351501", 0.085, None, None)])
    assert again.execute("SELECT COUNT(*) FROM watch").fetchone()[0] == 1
    assert costs(again)["00378351501"] == 0.085

    # importing a cost for an unwatched NDC adds it
    watch_cost(again, [("99999999999", 1.5, None, None)])
    assert len(watched(again)) == 2
    again.close()


def tquantitysurvivesacostsonlyimport(tmp_path):

    from pharmacli.store import costs, stock, watch_cost

    conn = connect(str(tmp_path / "q.db"))
    watch_cost(conn, [("00378351501", 0.09, 240, 60)])
    assert stock(conn)["00378351501"] == (240.0, 60.0)

    watch_cost(conn, [("00378351501", 0.085, None, None)])   # costs-only re-import
    assert costs(conn)["00378351501"] == 0.085               # cost updated
    assert stock(conn)["00378351501"] == (240.0, 60.0)       # quantity kept

    watch_cost(conn, [("00378351501", 0.085, 90, 60)])       # a real count
    assert stock(conn)["00378351501"] == (90.0, 60.0)
    conn.close()
