import json

import pandas as pd
import pytest
from conftest import row

from pharmacli.models.medicine import Medicine
from pharmacli.store import connect, insert

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

model = pytest.importorskip("pharmacli.model")
frame = pytest.importorskip("pharmacli.frame")
track = pytest.importorskip("pharmacli.track")

GOOD = "00000000001"      # steps +10% every other week -> the movers
FLAT = "00000000002"      # never moves -> the negatives


@pytest.fixture
def client(tmp_path, monkeypatch):
    import joblib

    db = str(tmp_path / "p.db")
    conn = connect(db)
    weeks = pd.date_range("2026-01-07", periods=20, freq="7D").strftime("%Y-%m-%d")
    meds = []
    for i, w in enumerate(weeks):
        meds.append(Medicine.arow(row(GOOD, f"{1.0 + (i // 2) * 0.1:.5f}", w), i))
        meds.append(Medicine.arow(row(FLAT, "5.00000", w), i))
    insert(conn, [m for m in meds if isinstance(m, Medicine)])
    conn.close()

    d = model.features(frame.load(db))
    X, y = d[model.NUMERIC + model.CATEGORICAL], d["y"]
    pipe = model.pipeline().fit(X, y)

    path = tmp_path / "m.joblib"
    joblib.dump(pipe, path)
    # the sidecar comes from track.sidecar(), not a hand-written dict, so a
    # change to its shape breaks this test instead of breaking production
    card = track.sidecar(pipe, X, {"version": 7, "run_id": "test", "data_md5": None,
                                   "threshold": 0.3, "val_pr_auc": 0.5,
                                   "test_pr_auc": 0.5, "params": {}})
    path.with_suffix(".json").write_text(json.dumps(card))

    monkeypatch.setenv("PHARMACLI_DB", db)
    monkeypatch.setenv("PHARMACLI_MODEL", str(path))
    monkeypatch.setenv("PHARMACLI_JWT_SECRET", "unit-test-secret")
    from pharmacli import serve
    with TestClient(serve.app) as c:      # TestClient runs lifespan
        # week 10: /predict is behind a bearer token. tests/test_api.py is
        # where the token itself is tested; here it is just the way in
        c.headers["Authorization"] = f"Bearer {serve.issue('test')}"
        yield c


def thealthreportstheversionitisserving(client):
    b = client.get("/health").json()
    assert b["status"] == "ok"
    assert b["model"] == "pharmacli-mover/7"     # straight from the sidecar
    assert b["ndcs"] == 2


def tpredictscoresaknownndc(client):
    b = client.post("/predict", json={"ndc": GOOD}).json()
    assert 0.0 <= b["p_moves"] <= 1.0
    assert b["verdict"] in ("buy ahead", "hold")
    assert b["model"] == "pharmacli-mover/7"     # the version used, per the brief


def tmalformedndcisrejectedbyschema(client):
    # layer 1: Pydantic's pattern, before any of our code runs
    r = client.post("/predict", json={"ndc": "123"})
    assert r.status_code == 422
    assert "ndc" in json.dumps(r.json())


def tunknownndcis404withahelpfulmessage(client):
    r = client.post("/predict", json={"ndc": "99999999999"})
    assert r.status_code == 404
    assert "pharmacli fetch" in r.json()["detail"]      # tells you what to do


def timpossiblepriceisrejected(client):
    # layer 1 again, on an override: a price of zero or less is the same rule
    # Medicine.arow already enforces on the CSV side
    r = client.post("/predict", json={"ndc": GOOD, "overrides": {"unit_price": -5}})
    assert r.status_code == 422


def tageoutsidethetrainedrangeisrejected(client):
    # layer 3: schema-valid, in-range for Pydantic, but nothing like the data
    # the model saw. Refusing beats answering confidently.
    r = client.post("/predict", json={"ndc": GOOD, "overrides": {"age_days": 3000}})
    assert r.status_code == 422
    assert "trained on" in r.json()["detail"]


def tarefusalnamesthefeatureanditsrange(client):
    detail = client.post("/predict",
                         json={"ndc": GOOD, "overrides": {"age_days": 3000}}).json()["detail"]
    assert "age_days" in detail and "3000" in detail
