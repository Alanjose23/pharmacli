"""Regenerate the committed demo fixture: a small panel, and a model trained on it.

    python tests/make_demo.py --from pharmacli.db

`/predict` needs a panel and a promoted model. Both are gitignored and neither is
in the image, so a clean clone used to answer 503 -- the one part of the stack a
reviewer could not see work. These two files are small enough to commit, which is
what makes `docker compose up` a complete demo.

Only the public `nadac` table is copied. The `watch` table is deliberately left
behind: it holds your acquisition costs, and this file goes into git.

Regenerate when the feature list changes. tests/test_demo.py fails when the
committed model no longer matches the code.
"""

import argparse
import json
import pathlib
import random
import sqlite3

NAME = "pharmacli-mover-demo"          # so /health and every prediction say "demo"


def build_panel(source: str, out: str, n: int, seed: int) -> int:
    from pharmacli.store import connect

    pathlib.Path(out).unlink(missing_ok=True)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
        ndcs = [r[0] for r in src.execute("SELECT DISTINCT ndc FROM nadac")]
    pick = sorted(random.Random(seed).sample(ndcs, min(n, len(ndcs))))

    conn = connect(out)                # creates the current schema
    try:
        conn.execute("ATTACH DATABASE ? AS src", (source,))
        # by name, not SELECT *: a panel built by an older version of store.SCHEMA
        # carries columns this one dropped (pharmacy_type), and positional copy
        # would either fail or silently shift every value one column left
        want = {r[1] for r in conn.execute("PRAGMA table_info(nadac)")}
        cols = [r[1] for r in conn.execute("PRAGMA src.table_info(nadac)") if r[1] in want]
        holes = ",".join("?" * len(pick))
        conn.execute(f"INSERT INTO nadac ({','.join(cols)}) SELECT {','.join(cols)}"
                     f" FROM src.nadac WHERE ndc IN ({holes})", pick)
        conn.commit()
        conn.execute("DETACH DATABASE src")
        rows = conn.execute("SELECT COUNT(*) FROM nadac").fetchone()[0]
        conn.execute("VACUUM")
    finally:
        conn.close()
    return rows


def train(db: str, model_path: str) -> dict:
    import joblib

    from pharmacli import frame, model, track

    d = model.features(frame.load(db))
    Xtr, Xte, ytr, yte = model.temporal_split(d)
    pipe = model.pipeline().fit(Xtr, ytr)
    thr = model.best_threshold(pipe, Xtr, ytr)
    s = model.scores(yte, pipe.predict_proba(Xte)[:, 1], thr)

    card = track.sidecar(pipe, Xtr, {
        "version": 0, "run_id": "demo", "data_md5": None, "threshold": float(thr),
        "val_pr_auc": None, "test_pr_auc": float(s["PR-AUC"]), "params": {},
        # a reviewer reading the sidecar should not have to infer this
        "note": "demo fixture from tests/make_demo.py -- a sample of the panel, "
                "not the promoted model",
    })
    # sidecar() stamps the production name; this model is not that model
    card["name"] = NAME

    path = pathlib.Path(model_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipe, path)
    path.with_suffix(".json").write_text(json.dumps(card, indent=2))
    return {"train": len(Xtr), "test": len(Xte), "positives": int(ytr.sum()),
            "threshold": thr, "test_pr_auc": s["PR-AUC"]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="source", default="pharmacli.db", help="full panel to sample")
    ap.add_argument("--ndcs", type=int, default=300)
    ap.add_argument("--db", default="data/demo.db")
    ap.add_argument("--model", default="models/demo-mover.joblib")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    rows = build_panel(args.source, args.db, args.ndcs, args.seed)
    print(f"{args.db}: {rows:,} rows from {args.ndcs} NDCs "
          f"({pathlib.Path(args.db).stat().st_size / 2**20:.1f} MB)")
    out = train(args.db, args.model)
    print(f"{args.model}: trained on {out['train']:,} rows ({out['positives']:,} positive), "
          f"threshold {out['threshold']:.3f}, test PR-AUC {out['test_pr_auc']:.4f} "
          f"({pathlib.Path(args.model).stat().st_size / 2**20:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
