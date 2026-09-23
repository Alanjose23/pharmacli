from datetime import date
from decimal import Decimal

from pharmacli.models.medicine import Medicine, parsedate, parseotc, parseprice
from pharmacli.models.rows import COLUMNS, RawRow, SkippedRow
from pharmacli.store import connect, count, insert

GOOD = "STRIBILD TABLET,61958120101,150.40902,01/01/2026,EA,C/I,N,4,B,0.23432,05/20/2026,04/29/2026"


def row(line: str) -> RawRow:
    return dict(zip(COLUMNS, line.split(","), strict=True))

 # each rejection has its own reason
def tparseprice():
    assert parseprice("150.40902") == (Decimal("150.40902"), None)
    assert parseprice("") == (None, "missing price")
    assert parseprice("abc") == (None, "malformed price")
    assert parseprice("NaN") == (None, "malformed price")
    assert parseprice("-1") == (None, "non-positive price")
    assert parseprice("0") == (None, "non-positive price")


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
    again = Medicine.arow(dict(zip(COLUMNS, med.torow(), strict=True)), 2)
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
    conn = connect(path)
    watch_add(conn, ["00378351501"])
    conn.close()
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


# ── the clean pipeline, end to end through main() ────────────────────────────
# Added after a refactor renamed a local and left `len(clean.records)` behind on
# the --dryrun branch. It was a NameError on every dry run and the whole suite
# stayed green, because nothing exercised `pharmacli clean` as a command.
def _fixture_csv(tmp_path):
    from pharmacli.models.rows import COLUMNS

    body = ",".join(COLUMNS) + "\n"
    dupe = ("DRUG A,00000000001,1.00,01/01/2026,EA,C/I,N,1,G,,,01/08/2026\n")
    (tmp_path / "in.csv").write_text(body + dupe + dupe, encoding="utf-8")
    return str(tmp_path / "in.csv")


def tcleandryrunreportswithoutwriting(tmp_path):
    from pharmacli.cli import EXIT_OK, main

    src = _fixture_csv(tmp_path)
    out = tmp_path / "out.csv"
    assert main(["clean", src, "--rule", "dedup", "-n", "-o", str(out)]) == EXIT_OK
    assert not out.exists(), "--dryrun must not write"


def tcleanwritesthesurvivingrows(tmp_path):
    import csv

    from pharmacli.cli import EXIT_OK, main
    from pharmacli.models.rows import COLUMNS

    src = _fixture_csv(tmp_path)
    out = tmp_path / "out.csv"
    assert main(["clean", src, "--rule", "dedup", "-o", str(out)]) == EXIT_OK
    rows = list(csv.reader(out.open(newline="", encoding="utf-8")))
    assert rows[0] == list(COLUMNS)        # all 12 columns survive the round trip
    assert len(rows) == 2, "two identical rows in, one out"
    # Pharmacy Type Indicator is constant in NADAC and never stored in SQLite,
    # but a CSV round trip must still hand back what it was given
    assert rows[1][COLUMNS.index("Pharmacy Type Indicator")] == "C/I"


def tunknownrulesuggestsanearone(tmp_path):
    from pharmacli.cli import EXIT_BADRULE, main

    assert main(["clean", _fixture_csv(tmp_path), "--rule", "dedpu", "-n"]) == EXIT_BADRULE


def tsettingsaredocumentedwheretheyarelookedfor():
    from pathlib import Path

    from pharmacli.config import DEFAULTS

    root = Path(__file__).resolve().parent.parent
    for doc in (".env.example", "README.md"):
        text = (root / doc).read_text(encoding="utf-8")
        undocumented = [k for k in DEFAULTS if k not in text]
        assert not undocumented, f"{doc} does not mention {undocumented}"


def twatchrmactuallyremoves(tmp_path):
    from pharmacli.cli import EXIT_BADIN, EXIT_OK, main
    from pharmacli.store import connect, watched

    db = str(tmp_path / "w.db")
    assert main(["watch", "add", "00000000001", "00000000002", "--db", db]) == EXIT_OK
    assert main(["watch", "rm", "00000000001", "--db", db]) == EXIT_OK

    conn = connect(db)
    try:
        assert watched(conn) == ["00000000002"]
    finally:
        conn.close()

    # and it must not silently succeed with nothing to remove
    assert main(["watch", "rm", "--db", db]) == EXIT_BADIN


def tmissingextrasayssobutinternalbugsstillraise(monkeypatch):
    import pytest

    from pharmacli import cli

    def missing(name):
        def handler(args):
            raise ModuleNotFoundError(name=name)
        return handler

    monkeypatch.setitem(cli.HANDLERS, "columns", missing("pymupdf"))
    assert cli.main(["columns"]) == cli.EXIT_BADIN
    monkeypatch.setitem(cli.HANDLERS, "columns", missing("pharmacli.gone"))
    with pytest.raises(ModuleNotFoundError):
        cli.main(["columns"])


def tenvfileisreadwithoutpythondotenv(tmp_path):
    import os
    import subprocess
    import sys

    (tmp_path / ".env").write_text("# a comment\nPHARMACLI_DB=from_file.db\n"
                                   "export PHARMACLI_K = '9'\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PHARMACLI_")}
    out = subprocess.run([sys.executable, "-c",
                          "from pharmacli.config import load;"
                          " c = load(); print(c.db, c.k)"],
                         cwd=tmp_path, capture_output=True, text=True, env=env)
    assert out.stdout.strip() == "from_file.db 9", out.stderr

    # a real environment variable still beats the file
    out = subprocess.run([sys.executable, "-c",
                          "from pharmacli.config import load; print(load().db)"],
                         cwd=tmp_path, capture_output=True, text=True,
                         env={**env, "PHARMACLI_DB": "from_env.db"})
    assert out.stdout.strip() == "from_env.db", out.stderr
