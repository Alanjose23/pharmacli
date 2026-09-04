

import argparse
import json
import re

from conftest import row
from pharmacli.cli import cmddesk
from pharmacli.models.medicine import Medicine
from pharmacli.store import connect, insert, watch_cost


def build(tmp_path, limit=5000, **kw):
    db = str(tmp_path / "d.db")
    conn = connect(db)
    meds = [Medicine.arow(row(f"{i:011d}", f"{1.0 + i:.5f}", "2026-01-07"), i)
            for i in range(1, 40)]
    insert(conn, [m for m in meds if isinstance(m, Medicine)])
    # two of them are on the shelf, with cost and volume, as `watch import` leaves them
    watch_cost(conn, [("00000000005", 0.9, 300.0, 150.0),
                      ("00000000007", 1.1, 40.0, 60.0)])
    conn.close()

    out = tmp_path / "desk.html"
    args = argparse.Namespace(db=db, outfile=str(out), limit=limit, as_of=None,
                              pharmacy="Test Pharmacy", **kw)
    assert cmddesk(args) == 0
    page = out.read_text(encoding="utf-8")
    payload = json.loads(re.search(r"const DATA = (\{.*?\});\n", page, re.S).group(1))
    return page, payload


def tdeskembedsvalidjson(tmp_path):
    page, payload = build(tmp_path)
    assert "/*DATA*/" not in page          # placeholder actually substituted
    assert payload["pharmacy"] == "Test Pharmacy"
    assert payload["weekTotal"] == 39
    assert len(payload["drugs"]) == 39


def tstockedndcssurvivethelimit(tmp_path):
    """The sample is capped, but your own shelf is never what gets dropped --
    a desk missing the drugs you stock has no worklist to show."""
    _, payload = build(tmp_path, limit=3)
    ndcs = {d[0] for d in payload["drugs"]}
    assert {"00000000005", "00000000007"} <= ndcs

    # and they carry the cost and quantities the import recorded, not zeros
    shelf = {d[0]: d for d in payload["drugs"] if d[7]}
    assert shelf["00000000005"][7:10] == [300.0, 150.0, 0.9]
    assert shelf["00000000007"][7:10] == [40.0, 60.0, 1.1]


def tunstockeddrugscarrynoquantities(tmp_path):
    _, payload = build(tmp_path)
    other = next(d for d in payload["drugs"] if d[0] == "00000000009")
    assert other[7] == 0 and other[8] == 0 and other[9] == 0
