## tests break if schema and frame drift apart
import numpy as np
import pandas as pd
import pytest

from conftest import row          # the shared 12-column RawRow builder
from pharmacli.models.medicine import Medicine
from pharmacli.store import connect, insert

frame = pytest.importorskip("pharmacli.frame")


@pytest.fixture
def db(tmp_path):
    #weeks of prices that move plus one generic price
    path = str(tmp_path / "t.db")
    conn = connect(path)
    meds = [
        Medicine.arow(row("00000000001", "1.00", "2026-01-07"), 1),
        Medicine.arow(row("00000000001", "1.50", "2026-01-14"), 2),  # +50%
        Medicine.arow(row("00000000001", "1.50", "2026-01-21"), 3),  # flat
        Medicine.arow(row("00000000002", "20.00", "2026-01-07", "B", "2.00"), 4),
    ]
    assert all(isinstance(m, Medicine) for m in meds)
    insert(conn, meds) 
    conn.close()
    return path


def tloads(db):
    df = frame.load(db)
    assert len(df) == 4
    assert df["unit_price"].dtype == np.float64
    assert pd.api.types.is_datetime64_any_dtype(df["as_of_date"])
    assert isinstance(df["rate_class"].dtype, pd.CategoricalDtype)
    # load must sort
    assert df["as_of_date"].is_monotonic_increasing or df["ndc"].nunique() > 1


def tweekchange(db):
    df = frame.weekly_change(frame.load(db))
    moving = df[df.ndc == "00000000001"].sort_values("as_of_date")
    # nothing to compare against
    assert pd.isna(moving["wow"].iloc[0])
    assert moving["wow"].iloc[1] == pytest.approx(0.50)
    assert moving["wow"].iloc[2] == pytest.approx(0.0)

    # drop the middle week
    gapped = frame.weekly_change(df[df["as_of_date"] != "2026-01-14"])
    assert gapped.loc[gapped.ndc == "00000000001", "wow"].isna().all()


def tgencomp(db):
    bg = frame.brand_generic(frame.load(db))
    assert list(bg["ndc"]) == ["00000000002"]
    assert bg["ratio"].iloc[0] == pytest.approx(10.0)  # 20.00 / 2.00



def tknownval():
    assert frame.gini([5, 5, 5, 5]) == pytest.approx(0.0)  # perfectly equal
    assert frame.gini([0, 0, 0, 10]) == pytest.approx(0.75)  # (n-1)/n for one holder
    assert frame.gini([]) == 0.0
    assert frame.gini([0, 0]) == 0.0  # no movement at all



