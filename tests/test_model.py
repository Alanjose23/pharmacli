import numpy as np
import pandas as pd
import pytest

from conftest import row          # the shared 12-column RawRow builder
from pharmacli.models.medicine import Medicine
from pharmacli.store import connect, insert

model = pytest.importorskip("pharmacli.model")
frame = pytest.importorskip("pharmacli.frame")


@pytest.fixture
def panel(tmp_path):

    path = str(tmp_path / "p.db")
    conn = connect(path)
    weeks = pd.date_range("2026-01-07", periods=10, freq="7D").strftime("%Y-%m-%d")
    meds = []
    for i, w in enumerate(weeks):
        meds.append(Medicine.arow(row("00000000001", f"{1.0 + (i // 2) * 0.1:.5f}", w), i))
        meds.append(Medicine.arow(row("00000000002", "5.00000", w), i))
    insert(conn, [m for m in meds if isinstance(m, Medicine)])
    conn.close()
    return frame.load(path)


def test_target_looks_forward_features_look_back(panel):
    d = model.features(panel)
    one = d[d.ndc == "00000000001"].sort_values("as_of_date").reset_index(drop=True)
    # y at row t is |move| at row t+1 clearing the threshold. the fixture's move
    # is +50%, so it clears any sane threshold
    nxt = one.wow.shift(-1).abs()
    assert (one.y.to_numpy()[:-1] == (nxt >= model.THRESHOLD).to_numpy()[:-1]).all()


def tthresholdactuallygates(panel):

    hot = model.features(panel, threshold=0.01)   # 50% move clears 1%
    cold = model.features(panel, threshold=0.99)  # 50% move does not clear 99%
    assert hot.y.sum() > 0
    assert cold.y.sum() == 0


def tnonextweekisdroppednotnegative(panel):

    d = model.features(panel)


    # each NDC contributes one fewer row than it has weeks -- the last has no next
    per_ndc = d.groupby("ndc", observed=True).size()
    assert (per_ndc > 0).all()
    assert d.y.notna().all()          # no NaN survived into the label
    assert set(d.y.unique()) <= {0, 1}


def test_no_feature_contains_the_current_weeks_outcome(panel):
    d = model.features(panel)
    one = d[d.ndc == "00000000001"].sort_values("as_of_date")
    # moved_last is this week's `moved` shifted back one -- never this week's
    moved = ((one.wow != 0) & one.wow.notna()).to_numpy()
    assert np.array_equal(one.moved_last.to_numpy()[1:], moved[:-1].astype(float))


def test_split_shares_no_week_between_train_and_test(panel):
    d = model.features(panel)
    cutoff = "2026-02-11"
    tr_weeks = set(d[d.as_of_date < cutoff].as_of_date)
    te_weeks = set(d[d.as_of_date >= cutoff].as_of_date)
    assert tr_weeks and te_weeks
    # the whole point: no week may appear on both sides
    assert not (tr_weeks & te_weeks)


def test_split_is_temporal_not_random(panel):
    d = model.features(panel)
    Xtr, Xte, ytr, yte = model.temporal_split(d, cutoff="2026-02-11")
    assert len(Xtr) and len(Xte)
    assert list(Xtr.columns) == model.NUMERIC + model.CATEGORICAL
    # every training row must predate every test row
    assert d.loc[Xtr.index, "as_of_date"].max() < d.loc[Xte.index, "as_of_date"].min()


def test_pipeline_transformers_are_fit_only_on_train(panel):
    d = model.features(panel)
    Xtr, Xte, ytr, yte = model.temporal_split(d, cutoff="2026-02-11")
    pipe = model.pipeline().fit(Xtr, ytr)
    scaler = pipe.named_steps["pre"].named_transformers_["num"].named_steps["scale"]

    # it saw exactly the training rows, and no more
    assert scaler.n_samples_seen_ == len(Xtr)

    # and its mean is the TRAIN mean, not the train+test mean. constructed so
    # the two genuinely differ, otherwise this assertion proves nothing
    both = pd.concat([Xtr, Xte])[model.NUMERIC]
    train_mean = Xtr[model.NUMERIC].fillna(Xtr[model.NUMERIC].median()).mean()
    both_mean = both.fillna(both.median()).mean()
    assert not np.allclose(train_mean, both_mean), "fixture too uniform to test"
    assert np.allclose(scaler.mean_, train_mean)


def test_scores_reports_no_accuracy(panel):
    # accuracy on a 77%-negative target is misleading, so it must not appear
    s = model.scores(np.array([0, 1, 0, 1]), np.array([0.1, 0.9, 0.2, 0.8]))
    assert "accuracy" not in s
    assert s["PR-AUC"] == pytest.approx(1.0)


def tagedaysisnotaleak(panel):

    d = model.features(panel)
    expected = (d["as_of_date"] - d["effective_date"]).dt.days.astype(float)
    assert (d["age_days"] == expected).all()
    # effective_date is the price already in force, so age is non-negative here
    assert (d["age_days"] >= 0).all()


def tnomonotoniccounterfeatures():

    assert "tenure" not in model.NUMERIC
    assert "tenure" not in model.CATEGORICAL


def tdirectionsplitsthetarget(panel):

    both = model.features(panel, threshold=0.01)
    down = model.features(panel, threshold=0.01, direction="down")
    up = model.features(panel, threshold=0.01, direction="up")
    assert (down.y & up.y).sum() == 0                    # never both
    assert ((down.y | up.y) == both.y).all()             # together, the whole
    with pytest.raises(ValueError):
        model.features(panel, direction="sideways")


def tnetbenefitindexisusable():

    import numpy as np
    y = np.array([0, 1] * 50)
    nb = model.net_benefit(y, np.linspace(0, 1, 100))
    assert 0.15 in nb.index and 0.35 in nb.index
    # treat_none is the do-nothing floor and is zero by construction
    assert (nb.treat_none == 0).all()
