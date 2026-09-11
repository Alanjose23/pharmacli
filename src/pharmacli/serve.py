import logging
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from pharmacli import model
from pharmacli.config import load as load_config

log = logging.getLogger(__name__)

# module-level state, filled once by lifespan() at boot
S: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    from pharmacli.frame import load as load_frame

    cfg = load_config()
    path = Path(cfg.model_path)
    if not path.exists():
        raise RuntimeError(f"no promoted model at {path} -- run `pharmacli sweep` first")

    pipe, card = model.load_promoted(str(path))

    # Builds features over the FULL panel at boot (~8s since _cycle_features
    # was vectorized, was ~28s). Do NOT slice to a shorter window to speed it
    # up: weeks_since_move is a cumsum from each NDC's first row, so truncating
    # saturates it rather than shortening it. Measured at a 26-week window, 478
    # NDCs come out wrong -- and it is the STALEST drugs that break, which are
    # exactly the ones the feature exists to flag. It also only saved 5%,
    # because the cost was never row count.
    log.info("building panel from %s", cfg.db)
    everything = model.features(load_frame(cfg.db), labeled_only=False)
    # the newest week is unlabelled by definition -- its outcome has not
    # happened yet -- which is exactly the week worth scoring
    newest = everything["as_of_date"].max()
    live = everything[everything["as_of_date"] == newest].set_index("ndc")

    S.update(pipe=pipe, card=card, live=live[~live.index.duplicated()],
             week=str(newest.date()), version=f"{card['name']}/{card['version']}")
    log.info("serving %s over %d NDCs from %s", S["version"], len(S["live"]), S["week"])
    yield
    S.clear()


app = FastAPI(title="pharmacli", version="6.0", lifespan=lifespan,
              description="Will this NDC reprice in next week's NADAC file?")

# No CORS middleware. The only browser client was the file:// buying desk, whose
# "null" origin needed allow_origins=["*"]; with the desk gone that would widen
# the boundary for nobody. Add it back the day a real web client exists, scoped
# to that origin rather than "*".


# ── request / response ───────────────────────────────────────────────────────
class Overrides(BaseModel):
    unit_price: float | None = Field(None, gt=0, le=100_000,
                                     description="dollars per unit; must be positive")
    age_days: float | None = Field(None, ge=0, le=3650,
                                   description="days since the price took effect")


class PredictIn(BaseModel):
    # NADAC NDCs are 11 digits, zero-padded. Anything else is a typo, and
    # FastAPI rejects it with a field-level 422 before any of our code runs.
    ndc: str = Field(..., pattern=r"^\d{11}$", examples=["00093721410"])
    overrides: Overrides | None = None


@app.get("/health")
def health():
    c = S["card"]
    return {"status": "ok", "model": S["version"], "trained_at": c["trained_at"],
            "test_pr_auc": c["test_pr_auc"], "threshold": c["threshold"],
            "data_md5": c["data_md5"], "week": S["week"], "ndcs": len(S["live"])}


@app.post("/predict")
def predict(req: PredictIn):
    # Layer 2: is this a thing we know about?
    if req.ndc not in S["live"].index:
        raise HTTPException(404, f"NDC {req.ndc} is not in the {S['week']} NADAC "
                                 f"file. Run `pharmacli fetch` for a newer file, "
                                 f"or check the NDC.")
    row = S["live"].loc[req.ndc].copy()

    if req.overrides:
        for k, v in req.overrides.model_dump(exclude_none=True).items():
            row[k] = v
        # log_price is DERIVED from unit_price -- overriding one without the
        # other would feed the model a contradiction it cannot see.
        if req.overrides.unit_price is not None:
            row["log_price"] = float(np.log10(max(req.overrides.unit_price, 1e-6)))

    # Layer 3: is this row inside the world the model was trained on? A model
    # asked about a price 3,000 days old will answer confidently and be wrong;
    # refusing is more useful than a number nobody can act on.
    for col, (lo, hi) in S["card"]["bounds"].items():
        v = row.get(col)
        if v is not None and pd.notna(v) and not (lo <= float(v) <= hi):
            raise HTTPException(422, f"{col}={float(v):.4g} is outside the range this "
                                     f"model was trained on [{lo:.4g}, {hi:.4g}]. "
                                     f"Refusing rather than guessing.")

    # Unseen categoricals are warned about, not rejected: handle_unknown="ignore"
    # zeroes them, so the answer is valid, just built on less information.
    warnings = [f"{c}={row[c]!r} was not seen in training; prediction uses the "
                f"numeric features only for this field"
                for c, seen in S["card"]["categories"].items()
                if pd.notna(row.get(c)) and str(row[c]) not in seen]

    X = pd.DataFrame([row])[model.NUMERIC + model.CATEGORICAL]
    p = float(S["pipe"].predict_proba(X)[0, 1])
    thr = S["card"]["threshold"]
    return {
        "ndc": req.ndc, "description": str(row["description"]),
        "p_moves": round(p, 4), "threshold": round(thr, 4),
        "verdict": "buy ahead" if p >= thr else "hold",
        "unit_price": round(float(row["unit_price"]), 5),
        "age_days": None if pd.isna(row["age_days"]) else int(row["age_days"]),
        "as_of": S["week"], "model": S["version"], "warnings": warnings,
    }
