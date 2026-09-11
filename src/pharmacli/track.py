import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import joblib
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import ParameterGrid

from pharmacli import model
from pharmacli.config import load as load_config
from pharmacli.model import CATEGORICAL, NUMERIC, SEED

log = logging.getLogger(__name__)

# Where the promoted model lands. Two files, deliberately: the pickle (which
# only sklearn can read) and a JSON sidecar (which anything can read, including
# a human deciding whether to trust what is deployed).
MODEL_DIR = Path("models")
MODEL_NAME = "pharmacli-mover"

# One SQLite file holds runs AND the model registry. The plain-file MLflow
# backend has historically not supported the registry at all; a sqlite URI does,
# costs one string, and this project already lives in SQLite.
TRACKING_URI = "sqlite:///mlflow.db"

# ── the split ────────────────────────────────────────────────────────────────
# THREE ways, not two. model.CUTOFF splits train/test and the README quotes the
# test PR-AUC -- so selecting a winner on that same test set would be leakage
# dressed up as an experiment. Instead: fit on train, PICK on validation, and
# touch test exactly once, for the winner alone. VAL_CUTOFF is the only new
# date; test is still model.CUTOFF, so the headline number stays comparable to
# every number already in the README and the week-5 notebook.
VAL_CUTOFF = "2026-05-13"


def splits(d: pd.DataFrame):
    cols = NUMERIC + CATEGORICAL
    tr = d[d["as_of_date"] < VAL_CUTOFF]
    va = d[(d["as_of_date"] >= VAL_CUTOFF) & (d["as_of_date"] < model.CUTOFF)]
    te = d[d["as_of_date"] >= model.CUTOFF]
    return ((tr[cols], tr["y"]), (va[cols], va["y"]), (te[cols], te["y"]))


# ── the grid ─────────────────────────────────────────────────────────────────
# 9 + 2 = 11 model runs, plus 3 baseline runs logged below = 14 tracked runs.
# ParameterGrid is already installed with sklearn -- no new dependency, and it
# beats hand-listing eleven dicts. Two estimator families, because a sweep over
# one family only tells you about that family.
GRID = [
    *ParameterGrid({"est": ["hgb"], "learning_rate": [0.03, 0.1, 0.3],
                    "max_leaf_nodes": [15, 31, 63]}),
    *ParameterGrid({"est": ["logreg"], "C": [0.1, 1.0]}),
]


def build(cfg: dict):
    if cfg["est"] == "hgb":
        return HistGradientBoostingClassifier(
            max_iter=200, learning_rate=cfg["learning_rate"],
            max_leaf_nodes=cfg["max_leaf_nodes"], random_state=SEED)
    # lbfgs on ~500k rows needs the higher iteration cap to converge; the
    # numerics are already StandardScaler'd inside the Pipeline.
    return LogisticRegression(C=cfg["C"], max_iter=1000, random_state=SEED)


def data_md5(db: str | None = None) -> str:
    from pharmacli.store import connect, panel_fingerprint

    conn = connect(db or load_config().db)
    try:
        return panel_fingerprint(conn)
    finally:
        conn.close()


# ── the promoted artifact ────────────────────────────────────────────────────
def sidecar(fitted, Xtr, meta: dict) -> dict:
    # min/max of the training data, widened 20%. Percentiles would reject the
    # legitimate tails of a real distribution; raw min/max would reject a new
    # drug whose price age is one day past anything seen. The margin admits
    # normal drift and still catches the genuinely impossible (age_days=5000).
    lo, hi = Xtr[NUMERIC].min(), Xtr[NUMERIC].max()
    pad = (hi - lo).abs() * 0.2
    # Never pad a non-negative feature below zero: moves_8w and weeks_since_move
    # are counts, and a lower bound of -0.6 on a count reads as a bug and would
    # wave through an impossible value.
    #
    # age_days is deliberately NOT in that group -- its real training minimum is
    # -5. CMS pre-publishes a handful of prices with an effective date after the
    # file that carries them (23 rows of 995,678, all ZEPBOUND), so a negative
    # price age is a real thing this panel contains, not dirty data. Clamping it
    # to zero would reject rows the model was actually trained on.
    low = (lo - pad).where(lo < 0, (lo - pad).clip(lower=0))
    high = hi + pad
    hot = fitted.named_steps["pre"].named_transformers_["cat"].named_steps["hot"]
    return {
        **meta,
        "name": MODEL_NAME,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": SEED,
        "features": {"numeric": NUMERIC, "categorical": CATEGORICAL},
        # low/high are already padded above -- do not subtract pad again here
        "bounds": {c: [float(low[c]), float(high[c])] for c in NUMERIC},
        # unseen categories are warned about, not rejected: the OneHotEncoder is
        # handle_unknown="ignore", so the prediction is valid, only weaker.
        "categories": {c: [str(v) for v in cats]
                       for c, cats in zip(CATEGORICAL, hot.categories_)},
    }


def main(db: str | None = None, sample: int | None = None) -> int:
    # MLflow posts usage telemetry to a third-party endpoint on import. Nobody
    # asked for run metadata to leave this machine; opt out before the import.
    # setdefault, so an explicit env var still wins.
    import os
    os.environ.setdefault("MLFLOW_DISABLE_TELEMETRY", "1")

    import mlflow                                  # heavy; dev-only, never in serve
    from pharmacli.frame import load as load_frame

    mlflow.set_tracking_uri(TRACKING_URI)
    mlflow.set_experiment("pharmacli-mover")

    log.info("building the panel (this takes a minute on the full file)")
    df = load_frame(db)
    if sample:                                     # --sample: same code path, fewer
        keep = pd.Index(df["ndc"].unique()[:sample])   # NDCs. Makes the sweep
        df = df[df["ndc"].isin(keep)]                  # runnable in a test.
    d = model.features(df)
    (Xtr, ytr), (Xva, yva), (Xte, yte) = splits(d)
    md5 = data_md5(db)
    log.info("train %d / val %d / test %d rows", len(Xtr), len(Xva), len(Xte))

    # shared by every run, so runs are comparable in the MLflow UI
    common = {"seed": SEED, "data_md5": md5, "n_train": len(Xtr),
              "n_val": len(Xva), "val_cutoff": VAL_CUTOFF, "test_cutoff": model.CUTOFF,
              "threshold_pct": model.THRESHOLD}

    # ── baselines as runs ────────────────────────────────────────────────────
    # model.baselines() already computes these. Logging them as RUNS means the
    # UI comparison shows the model beating the honest bar (calendar_only), not
    # just the trivial one. A sweep that only compares tuned models to each
    # other cannot tell you whether any of them were worth training.
    log.info("logging 3 baselines")
    bars = model.baselines(Xtr, Xte, ytr, yte)
    for name, row in bars.iterrows():
        with mlflow.start_run(run_name=f"baseline-{name}"):
            mlflow.log_params({**common, "est": "baseline", "kind": name})
            mlflow.log_metrics({k: float(v) for k, v in row.items()})

    # ── the sweep ────────────────────────────────────────────────────────────
    best = None
    for i, cfg in enumerate(GRID, 1):
        tag = f"{cfg['est']}-{i:02d}" + (f"-sample{sample}" if sample else "")
        with mlflow.start_run(run_name=tag) as run:
            pipe = model.pipeline(build(cfg)).fit(Xtr, ytr)
            proba = pipe.predict_proba(Xva)[:, 1]
            m = model.scores(yva, proba)
            mlflow.log_params({**common, **cfg})
            mlflow.log_metrics({f"val_{k}": float(v) for k, v in m.items()})
            log.info("%2d/%d %-8s %s val PR-AUC %.4f", i, len(GRID), cfg["est"],
                     {k: v for k, v in cfg.items() if k != "est"}, m["PR-AUC"])
            # selection happens HERE, on validation, and nowhere else
            if best is None or m["PR-AUC"] > best[0]:
                best = (m["PR-AUC"], cfg, pipe, run.info.run_id)

    # ── promote ──────────────────────────────────────────────────────────────
    val_auc, cfg, pipe, run_id = best

    # A --sample run trains on a slice and scores accordingly -- 400 NDCs gave
    # test PR-AUC 0.0032 against the full panel's 0.4077. Promoting that would
    # overwrite the champion that `pharmacli forecast` and the API load, from a
    # command whose whole purpose is to be a fast smoke test. It did, once.
    if sample:
        log.info("winner: %s val PR-AUC %.4f -- NOT promoted (--sample run)",
                 cfg, val_auc)
        log.info("runs are in MLflow; re-run without --sample to promote")
        return 0

    log.info("winner: %s val PR-AUC %.4f -- scoring test once", cfg, val_auc)
    test = model.scores(yte, pipe.predict_proba(Xte)[:, 1])
    thr = model.best_threshold(pipe, Xtr, ytr)

    with mlflow.start_run(run_id=run_id):          # reopen the winner's run
        mlflow.log_metrics({f"test_{k}": float(v) for k, v in test.items()})
        mlflow.log_metric("decision_threshold", thr)
        # MLflow 3 serialises with skops, not pickle, and skops refuses any type
        # not on an explicit allow-list -- which is the correct default and the
        # reason the registry copy is safer than the joblib export beside it.
        # numpy.dtype is a plain type descriptor carrying no code; declaring it
        # trusted is a decision recorded here rather than a blanket opt-out.
        info = mlflow.sklearn.log_model(pipe, name="model",
                                        registered_model_name=MODEL_NAME,
                                        skops_trusted_types=["numpy.dtype"])
    # log_model returns the registered version directly; get_latest_versions is
    # tied to the stage API that MLflow 3 removed.
    version = info.registered_model_version
    # "champion" is the alias the registry serves -- stages are gone in 3.x
    mlflow.MlflowClient().set_registered_model_alias(MODEL_NAME, "champion", version)

    # ponytail: the joblib export is a COPY of registry version N. It exists so
    # serve.py and `pharmacli forecast` need no mlflow install. Re-run the sweep
    # to refresh it. If the registry ever gets a second writer, load from
    # models:/pharmacli-mover@champion directly and delete this export.
    MODEL_DIR.mkdir(exist_ok=True)
    joblib.dump(pipe, MODEL_DIR / f"{MODEL_NAME}.joblib")
    card = sidecar(pipe, Xtr, {
        "version": int(version), "run_id": run_id, "data_md5": md5,
        "threshold": float(thr), "val_pr_auc": float(val_auc),
        "test_pr_auc": float(test["PR-AUC"]), "params": cfg,
        # THE bar this model has to clear -- same estimator, calendar features
        # only. Beating always_no is trivial (the class is 4%); beating this is
        # the claim worth making, so it ships with the model that made it.
        "baseline_pr_auc": float(bars.loc["calendar_only", "PR-AUC"]),
        "baseline_name": "calendar_only",
    })
    (MODEL_DIR / f"{MODEL_NAME}.json").write_text(json.dumps(card, indent=2))

    log.info("promoted %s v%s -- val %.4f, test %.4f, threshold %.3f",
             MODEL_NAME, version, val_auc, test["PR-AUC"], thr)
    log.info("compare runs: mlflow ui --backend-store-uri %s", TRACKING_URI)
    return 0
