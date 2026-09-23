#   1. the target looks FORWARD one week; every feature looks BACKWARD
#   2. the split is by time, never random -- the same NDC appears in all 34
#      weeks, so a random split puts adjacent (77% identical) weeks on both
#      sides and scores beautifully while meaning nothing
#   3. every transformer is fit inside the Pipeline, so imputation and scaling
#      never see the test weeks

# target looks forward one week
# split is by time, same ndc appears weeks on end

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from pharmacli.frame import weekly_change

NUMERIC = ["log_price", "moved_last", "moves_8w", "vol_4w",
           "weeks_since_move", "age_days", "day_of_month", "is_monthly_week"]
CATEGORICAL = ["rate_class", "otc", "pricing_unit"]


# Every source of randomness in this module. The sweep logs it as an MLflow
# param, so a run can be replayed exactly. Change this and every tracked
# metric shifts.
SEED = 0

CUTOFF = "2026-07-01"

THRESHOLD = 0.05


def _cycle_features(d: pd.DataFrame, g) -> pd.DataFrame:
    # REQUIRES d sorted by ndc -- groupby().rolling() returns a group-sorted
    # MultiIndex and reset_index(level=0, drop=True) only lines back up with the
    # caller because features() -> weekly_change() sorts first. Call this on an
    # unsorted frame and the two rolling columns silently misalign.
    k = d["ndc"]
    d["moved_last"] = g["moved"].shift(1)

    # groupby().rolling() rather than groupby().transform(lambda). Identical
    # output -- verified bit-for-bit on all 1,028,250 rows x 4 features -- but
    # the lambda ran the Python interpreter once per NDC across 32,509 groups
    # and cost 21.7 of features()'s 21.9 seconds. Cython does it in 1.8.
    # abs() before shift() rather than after: the two commute, and shifting the
    # grouped series first is what keeps this on the vectorized path.
    d["moves_8w"] = (g["moved"].shift(1).groupby(k, observed=True)
                     .rolling(8, min_periods=1).sum()
                     .reset_index(level=0, drop=True))
    d["vol_4w"] = (g["wow"].shift(1).abs().groupby(k, observed=True)
                   .rolling(4, min_periods=1).mean()
                   .reset_index(level=0, drop=True))

    # NADAC reprices on a MONTHLY cycle
    prev = g["moved"].shift(1).fillna(False).astype(int)  # cumsum needs numeric
    cycle = prev.groupby(k, observed=True).cumsum()
    d["weeks_since_move"] = d.groupby([k, cycle], observed=True).cumcount()
    return d


def features(df: pd.DataFrame, threshold: float = THRESHOLD,
             labeled_only: bool = True, direction: str | None = None) -> pd.DataFrame:
    d = weekly_change(df)
    g = d.groupby("ndc", observed=True)

    # `wow` at week t is the move from t-1 to t, so `moved` is backward-looking.

    d["moved"] = (d["wow"] != 0) & d["wow"].notna()

    #
    nxt = g["wow"].shift(-1)

    if direction == "down":
        hit = nxt <= -threshold
    elif direction == "up":
        hit = nxt >= threshold
    elif direction is None:
        hit = nxt.abs() >= threshold
    else:
        raise ValueError(f"direction must be None, 'down' or 'up', got {direction!r}")
    d["y"] = hit.where(nxt.notna())

    # every feature is shift(1)-ed first
    d = _cycle_features(d, g)
    d["log_price"] = np.log10(d["unit_price"].clip(lower=1e-6))

    # how old the price currently in force is
    d["age_days"] = (d["as_of_date"] - d["effective_date"]).dt.days

    # CMS publishes the NADAC *Monthly* file on the first Monday on or after the 15th
    d["day_of_month"] = d["as_of_date"].dt.day
    d["is_monthly_week"] = ((d["day_of_month"] >= 15)
                            & (d["day_of_month"] <= 21)).astype(int)

    # shifting a boolean yields object dtype with NaN
    d[NUMERIC] = d[NUMERIC].astype(float)


    if not labeled_only:
        return d
    return d.dropna(subset=["y"]).assign(y=lambda x: x["y"].astype(int))


def temporal_split(d: pd.DataFrame, cutoff: str = CUTOFF):

    train = d[d["as_of_date"] < cutoff]
    test = d[d["as_of_date"] >= cutoff]
    cols = NUMERIC + CATEGORICAL
    return train[cols], test[cols], train["y"], test["y"]


def pipeline(est=None) -> Pipeline:
    pre = ColumnTransformer([
        ("num", Pipeline([("impute", SimpleImputer(strategy="median")),
                          ("scale", StandardScaler())]), NUMERIC),
        ("cat", Pipeline([("impute", SimpleImputer(strategy="most_frequent")),
                          ("hot", OneHotEncoder(handle_unknown="ignore",
                                                min_frequency=20))]), CATEGORICAL),
    ])
    # HistGradientBoosting rather than XGBoost/LightGBM: already in sklearn
    return Pipeline([("pre", pre),
                     ("est", est or HistGradientBoostingClassifier(
                         max_iter=200, learning_rate=0.1, random_state=SEED))])


def scores(y_true, proba, threshold: float = 0.5) -> dict:
    pred = (proba >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_true, pred, labels=[0, 1]).ravel()
    return {
        "PR-AUC": average_precision_score(y_true, proba),
        "ROC-AUC": roc_auc_score(y_true, proba),
        "precision": precision_score(y_true, pred, zero_division=0),
        "recall": recall_score(y_true, pred, zero_division=0),
        "F1": f1_score(y_true, pred, zero_division=0),
        "Brier": brier_score_loss(y_true, proba),
        "TN": tn, "FP": fp, "FN": fn, "TP": tp,
    }


def baselines(Xtr, Xte, ytr, yte) -> pd.DataFrame:
    dummy = DummyClassifier(strategy="most_frequent").fit(Xtr, ytr)
    rows = {
        "always_no": scores(yte, dummy.predict_proba(Xte)[:, 1]),
        # a rule, not a model: reuse the lagged feature directly as the score
        "persistence": scores(yte, Xte["moved_last"].fillna(0).to_numpy(dtype=float)),
        # THE bar. NADAC reprices on a published CMS schedule
        "calendar_only": scores(yte, _calendar_only(Xtr, Xte, ytr)),
    }
    return pd.DataFrame(rows).T


CALENDAR = ["weeks_since_move", "age_days", "day_of_month", "is_monthly_week"]


def _calendar_only(Xtr, Xte, ytr) -> np.ndarray:
    pre = ColumnTransformer([("num", SimpleImputer(strategy="median"), CALENDAR)])
    pipe = Pipeline([("pre", pre),
                     ("est", HistGradientBoostingClassifier(random_state=SEED))])
    return pipe.fit(Xtr[CALENDAR], ytr).predict_proba(Xte[CALENDAR])[:, 1]


def best_threshold(fitted: Pipeline, Xtr, ytr) -> float:

    pr, rc, th = precision_recall_curve(ytr, fitted.predict_proba(Xtr)[:, 1])
    f1 = 2 * pr * rc / (pr + rc + 1e-12)
    return float(th[int(np.nanargmax(f1[:-1]))])


def net_benefit(y_true, proba) -> pd.DataFrame:
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(proba, dtype=float)
    n = len(y)
    rows = []
    # fixed sweep: the `thresholds` parameter had no caller, and the band that
    # matters here (0.05-0.40) sits well inside this range
    for t in np.arange(0.05, 0.96, 0.05):
        pred = p >= t
        tp = float((pred & (y == 1)).sum())
        fp = float((pred & (y == 0)).sum())
        odds = t / (1 - t)
        rows.append({
            # rounded: np.arange gives 0.15000000000000002, and a frame indexed
            # by that cannot be looked up by the threshold you asked for
            "threshold": round(float(t), 2),
            "model": tp / n - (fp / n) * odds,
            # buy ahead on everything: every negative is a false positive
            "treat_all": y.mean() - (1 - y.mean()) * odds,
            "treat_none": 0.0,
        })
    return pd.DataFrame(rows).set_index("threshold")


def explain(fitted: Pipeline, X: pd.DataFrame, n: int = 2000):
    ## sample for pipeline, imported
    import shap

    sample = X.sample(min(n, len(X)), random_state=0)
    mat = fitted.named_steps["pre"].transform(sample)
    names = fitted.named_steps["pre"].get_feature_names_out()
    ex = shap.TreeExplainer(fitted.named_steps["est"])
    return ex(np.asarray(mat, dtype=float)), names, sample


# ── the promoted model ───────────────────────────────────────────────────────
# track.py writes these two files; cli.py and serve.py read them. Nothing here
# imports mlflow: serving a model must not require the machinery that trained it.
#
# SECURITY -- joblib.load() executes arbitrary code in the pickle stream. That is
# what pickle IS, not a bug to patch. The mitigation is a boundary: the path
# comes from config, never from user input, and the result is type-checked.
# Never point this at a model file someone sent you.
def load_promoted(path: str) -> tuple[Pipeline, dict]:
    import json
    from pathlib import Path

    import joblib

    p = Path(path)
    pipe = joblib.load(p)                      # <- the trust boundary
    if not isinstance(pipe, Pipeline):
        raise RuntimeError(f"{p} holds a {type(pipe).__name__}, not a sklearn Pipeline")
    return pipe, json.loads(p.with_suffix(".json").read_text())
