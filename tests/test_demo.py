# The committed demo fixture is what makes /predict answer on a clean clone, and
# it is a trained pickle: change the feature list, the sidecar shape or the
# pipeline and it goes stale silently. These tests fail instead, naming the fix.
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "demo.db"
MODEL = ROOT / "models" / "demo-mover.joblib"
REBUILD = "run `python tests/make_demo.py --from pharmacli.db` to rebuild it"


@pytest.fixture
def demo(monkeypatch):
    assert DB.exists() and MODEL.exists(), f"the demo fixture is missing -- {REBUILD}"
    monkeypatch.setenv("PHARMACLI_DB", str(DB))
    monkeypatch.setenv("PHARMACLI_MODEL", str(MODEL))
    monkeypatch.setenv("PHARMACLI_JWT_SECRET", "unit-test-secret")
    from pharmacli import serve
    with TestClient(serve.app) as c:
        c.headers["Authorization"] = f"Bearer {serve.issue('test')}"
        yield c


def _an_ndc() -> str:
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as conn:
        newest = conn.execute("SELECT MAX(as_of_date) FROM nadac").fetchone()[0]
        return conn.execute("SELECT ndc FROM nadac WHERE as_of_date = ? LIMIT 1",
                            (newest,)).fetchone()[0]


def tthefixturemodelstillloads(demo):
    b = demo.get("/health").json()
    # /health carries model_error only when the load failed
    assert "model_error" not in b, f"{b.get('model_error')} -- {REBUILD}"
    # the name says demo, so nobody mistakes these predictions for the real model
    assert b["model"] == "pharmacli-mover-demo/0"
    assert b["ndcs"] > 0


def tthefixtureanswersapredict(demo):
    r = demo.post("/predict", json={"ndc": _an_ndc()})
    assert r.status_code == 200, f"{r.json()} -- {REBUILD}"
    b = r.json()
    assert 0.0 <= b["p_moves"] <= 1.0
    assert b["verdict"] in ("buy ahead", "hold")
    assert b["model"] == "pharmacli-mover-demo/0"
