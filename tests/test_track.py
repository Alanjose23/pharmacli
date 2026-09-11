import pandas as pd
import pytest

model = pytest.importorskip("pharmacli.model")
track = pytest.importorskip("pharmacli.track")


def tgridsweepsatleasteightruns():
    # the brief asks for 8-10 tracked runs; baselines add 3 more on top
    assert len(track.GRID) >= 8
    assert {c["est"] for c in track.GRID} == {"hgb", "logreg"}, "sweep one family, learn one family"


def tpipelineacceptseachsweptestimator():
    for cfg in track.GRID:
        pipe = model.pipeline(track.build(cfg))
        assert pipe.named_steps["est"].__class__.__name__ in (
            "HistGradientBoostingClassifier", "LogisticRegression")


def tsplitsaredisjointandordered():
    weeks = pd.date_range("2026-01-07", periods=34, freq="7D")
    d = pd.DataFrame({"as_of_date": weeks, "y": 0,
                      **{c: 0.0 for c in model.NUMERIC},
                      **{c: "G" for c in model.CATEGORICAL}})
    (Xtr, _), (Xva, _), (Xte, _) = track.splits(d)
    assert len(Xtr) and len(Xva) and len(Xte), "every split must be non-empty"
    assert len(Xtr) + len(Xva) + len(Xte) == len(d), "no row in two splits"
    assert track.VAL_CUTOFF < model.CUTOFF, "validation must end before test begins"


def tpanelfingerprintignorestheshelf(tmp_path):
    from conftest import row
    from pharmacli.models.medicine import Medicine
    from pharmacli.store import (connect, insert, panel_fingerprint,
                                 watch_add, watch_cost, watch_remove)

    conn = connect(str(tmp_path / "f.db"))
    try:
        insert(conn, [m for m in
                      [Medicine.arow(row("00000000001", "1.00000", "2026-01-07"), 1)]
                      if isinstance(m, Medicine)])
        base = panel_fingerprint(conn)

        # shelf churn: none of this is training data
        watch_add(conn, ["00000000001"])
        watch_cost(conn, [("00000000001", 2.5, 10.0, 5.0)])
        assert panel_fingerprint(conn) == base, "a shelf edit changed the panel identity"
        watch_remove(conn, ["00000000001"])
        assert panel_fingerprint(conn) == base

        # a real panel change must move it
        insert(conn, [m for m in
                      [Medicine.arow(row("00000000002", "9.00000", "2026-01-14"), 2)]
                      if isinstance(m, Medicine)])
        assert panel_fingerprint(conn) != base, "a new NADAC row did not move the fingerprint"
    finally:
        conn.close()
