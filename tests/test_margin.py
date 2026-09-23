import argparse
import csv

from conftest import row

from pharmacli.cli import EXIT_OK, cmdmargin
from pharmacli.models.medicine import Medicine
from pharmacli.store import connect, insert, watch_cost

WEEK = "2026-01-07"
SINKING = "00000000001"   # costs more than NADAC -- belongs in the pack
FINE = "00000000002"      # costs less -- must not appear


def pack(tmp_path):
    db = str(tmp_path / "m.db")
    conn = connect(db)
    meds = [Medicine.arow(row(SINKING, "1.00000", WEEK), 1),
            # a comma in the description: unquoted, it shifts every later column
            Medicine.arow(row(FINE, "5.00000", WEEK), 2)]
    insert(conn, [m for m in meds if isinstance(m, Medicine)])
    watch_cost(conn, [(SINKING, 1.50, 100.0, 200.0),    # $0.50/unit underwater
                      (FINE, 1.00, 100.0, 200.0)])      # $4.00/unit to the good
    conn.close()

    out = tmp_path / "appeal.csv"
    args = argparse.Namespace(db=db, outfile=str(out), top=25)
    assert cmdmargin(args) == EXIT_OK
    with open(out, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def tonlyunderwaterlinesarefiled(tmp_path):
    rows = pack(tmp_path)
    # a pack padded with lines you profit on invites the PBM to reject all of it
    assert [r["ndc"] for r in rows] == [SINKING]


def teveryrowcarriesthenadacfiledate(tmp_path):
    # an appeal argued from an unnamed file is unarguable
    assert all(r["nadac_file"] == WEEK for r in pack(tmp_path))


def tthegapandlossareyourrealnumbers(tmp_path):
    r = pack(tmp_path)[0]
    assert float(r["your_cost"]) == 1.50
    assert float(r["nadac_unit"]) == 1.00
    assert float(r["gap_per_unit"]) == 0.50
    # 200 units a month at $0.50 under = $100 of real money
    assert float(r["monthly_loss"]) == 100.00


def tbreakevenquantityispresent(tmp_path):
    # units per script at which the dispensing fee stops covering the gap
    assert pack(tmp_path)[0]["breakeven_qty"]


# ── the Monday index ─────────────────────────────────────────────────────────
def ttodayranksappealaboveburiedtiming(tmp_path, capsys):
    import argparse

    from pharmacli.cli import EXIT_OK, cmdtoday

    db = str(tmp_path / "t.db")
    conn = connect(db)
    meds = [Medicine.arow(row(SINKING, "1.00000", WEEK), 1),
            Medicine.arow(row(FINE, "5.00000", WEEK), 2)]
    insert(conn, [m for m in meds if isinstance(m, Medicine)])
    watch_cost(conn, [(SINKING, 1.50, 100.0, 200.0),   # underwater
                      (FINE, 1.00, 100.0, 200.0)])     # fine
    conn.close()

    assert cmdtoday(argparse.Namespace(db=db, within=21, dead_days=180)) == EXIT_OK
    out = capsys.readouterr().out
    assert out.index("APPEAL") < out.index("BUY") < out.index("CLEAR")
    assert "1 positions cost more than NADAC" in out
    assert "$100.00/month" in out            # $0.50/unit under, 200 units a month
    assert "pharmacli margin -o" in out      # points at the command that does it


def ttodaysaysnothingdeadratherthanprintingnothing(tmp_path, capsys):
    import argparse

    from pharmacli.cli import EXIT_OK, cmdtoday

    db = str(tmp_path / "t.db")
    conn = connect(db)
    insert(conn, [m for m in [Medicine.arow(row(FINE, "5.00000", WEEK), 1)]
                  if isinstance(m, Medicine)])
    watch_cost(conn, [(FINE, 1.00, 100.0, 200.0)])     # 15 days of supply
    conn.close()

    assert cmdtoday(argparse.Namespace(db=db, within=21, dead_days=180)) == EXIT_OK
    out = capsys.readouterr().out
    assert "nothing dead" in out
    assert "still in the newest file" in out
    assert "nothing underwater" in out
