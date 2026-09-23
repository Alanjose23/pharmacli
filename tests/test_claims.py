import csv

from conftest import row

from pharmacli import claims
from pharmacli.models.medicine import Medicine
from pharmacli.store import connect, insert, watch_cost

A, B, C, G = "00000000001", "00000000002", "00000000003", "00000000004"


def panel(tmp_path):
    db = str(tmp_path / "p.db")
    conn = connect(db)
    meds = [Medicine.arow(row(A, "1.00000", "2026-01-07"), 1),
            Medicine.arow(row(A, "2.00000", "2026-01-14"), 2),          # A reprices up on the 14th
            Medicine.arow(row(B, "10.00000", "2026-01-14", rate="B", generic="4.00000"), 3),
            Medicine.arow(row(C, "3.00000", "2026-01-14", rate="B-ANDA", generic="1.00000"), 4),
            Medicine.arow(row(G, "5.00000", "2026-01-14", rate="G", generic="1.00000"), 5)]
    insert(conn, [m for m in meds if isinstance(m, Medicine)])
    watch_cost(conn, [(A, 1.50, 100.0, 60.0), (B, 9.00, 10.0, 30.0),
                      (C, 2.50, 10.0, 200.0), (G, 1.00, 10.0, 30.0)])
    conn.close()
    return db


def tsampleissamebytesforsameseed(tmp_path):
    db = panel(tmp_path)

    def make(seed, name):
        n = claims.sample(db, str(tmp_path / name), str(tmp_path / "payers.csv"), seed=seed)
        return n, (tmp_path / name).read_bytes()

    n1, one = make(7, "a.csv")
    n2, two = make(7, "b.csv")
    _, other = make(8, "c.csv")
    assert n1 == n2 > 0 and one == two and one != other
    with open(tmp_path / "payers.csv", newline="") as fh:
        payers = list(csv.reader(fh))
    assert payers[0] == ["payer", "dispensing_fee"]
    assert all("(sample)" in p for p, _ in payers[1:])       # illustrative, and named so


def tcheckusesthefilldatepriceandpayerfees(tmp_path, monkeypatch):
    db = panel(tmp_path)
    monkeypatch.setenv("PHARMACLI_DISPENSING_FEE", "10.00")
    rows = [
        # NADAC was 1.00 on the 10th: benchmark 10.50, paid more than that
        {"date": "2026-01-10", "rx": "1", "ndc": A, "qty": 10.0, "paid": 15.0, "payer": "PBM A"},
        # NADAC 2.00 by the 20th: benchmark 20.50; ingredient 14.50 against cost 15.00
        {"date": "2026-01-20", "rx": "2", "ndc": A, "qty": 10.0, "paid": 15.0, "payer": "PBM A"},
        # payer not in the table: the default fee
        {"date": "2026-01-20", "rx": "3", "ndc": B, "qty": 1.0, "paid": 5.0, "payer": "Somebody"},
        {"date": "2026-01-20", "rx": "4", "ndc": "99999999999", "qty": 1.0,
         "paid": 1.0, "payer": "PBM A"},
    ]
    result = claims.check(db, rows, {"PBM A": 0.50})
    got = {r["rx"]: r for r in result["claims"]}
    assert result["unpriced"] == 1
    assert (got["1"]["nadac_unit"], got["1"]["shortfall"]) == (1.0, -4.5)
    assert ((got["2"]["nadac_unit"], got["2"]["shortfall"], got["2"]["below_cost_loss"])
            == (2.0, 5.5, 0.5))
    assert ((got["3"]["fee"], got["3"]["shortfall"], got["3"]["below_cost_loss"])
            == (10.0, 15.0, 14.0))

    lines = claims.report(result, skipped=1)
    assert lines[0] == "3 claims priced, 1 skipped, 1 unpriced"
    assert lines[-1] == f"next: pharmacli docs appeal {B}"         # the NDC short the most dollars
    assert claims.write_underpaid(result, str(tmp_path / "u.csv")) == 2


def treadclaimsskipsbadrowswithlinenumbers(tmp_path):
    p = tmp_path / "c.csv"
    p.write_text("date,rx,ndc,qty,paid,payer\n"
                 "2026-01-20,1,00000000001,10,15,PBM A\n"
                 "not-a-date,2,00000000001,10,15,X\n"
                 "2026-01-20,3,123,10,15,X\n"
                 "2026-01-20,4,00000000001,0,15,X\n")
    rows, skipped = claims.read_claims(str(p))
    assert [r["rx"] for r in rows] == ["1"]
    assert [line for line, _ in skipped] == [3, 4, 5]


def tgenericsrankbrandsbymonthlysaving(tmp_path):
    db = panel(tmp_path)
    rows = claims.generics(db)
    # C: gap 2 x 200/month beats B: gap 6 x 30/month; G has a generic price but is not a brand
    assert [r[0] for r in rows] == [C, B]
    lines = claims.generic_lines(rows)
    assert "$580.00/month" in lines[-2] and lines[-1] == claims.DAW
    assert claims.generics(str(tmp_path / "empty.db")).startswith("no NADAC data")


def tgenericsreadsanuploadedshelf(tmp_path):
    db = panel(tmp_path)
    # the saved shelf carries both brands; an uploaded file carrying only C
    # answers for C alone, with the file's monthly volume
    got = claims.generics(db, shelf=({C: 2.50}, {C: (10.0, 999.0)}))
    assert [r[0] for r in got] == [C]
    assert got[0][4] == 999.0
    assert claims.generics(db, shelf=({}, {})).startswith("no inventory recorded")
