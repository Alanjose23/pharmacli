import logging

import numpy as np

# lives in ask.py so find-a-drug works on a core install without numpy
from pharmacli.ask import keyword_search  # noqa: F401
from pharmacli.model import SEED

log = logging.getLogger(__name__)

# G is a multi-source generic; B / B-ANDA / B-BIO are all brand-side. Collapsing
# to a binary makes the comparison readable and keeps the rare classes (94 rows
# across B-ANDA and B-BIO) from dominating the error bars.
BRAND = ("B", "B-ANDA", "B-BIO")


def corpus(db: str | None = None) -> tuple[list[str], np.ndarray]:
    from pharmacli.store import connect

    conn = connect(db or _db())
    try:
        rows = conn.execute(
            "SELECT description, rate_class, COUNT(*) FROM nadac"
            " WHERE description != '' AND rate_class != ''"
            " GROUP BY description, rate_class").fetchall()
    finally:
        conn.close()
    # a description can carry more than one rate_class across labellers; take
    # the one it appears under most, so the label is the drug's usual class
    best: dict[str, tuple[int, str]] = {}
    for desc, rc, n in rows:
        if n > best.get(desc, (0, ""))[0]:
            best[desc] = (n, rc)
    texts = sorted(best)
    y = np.array([1 if best[t][1] in BRAND else 0 for t in texts])
    return texts, y


def _db() -> str:
    from pharmacli.config import load
    return load().db


def split(texts: list[str], y: np.ndarray, test_frac: float = 0.25):
    rng = np.random.default_rng(SEED)
    heads = np.array([t.split()[0] if t.split() else t for t in texts])
    uniq = np.unique(heads)
    rng.shuffle(uniq)
    held = set(uniq[:max(1, int(len(uniq) * test_frac))])
    te = np.array([h in held for h in heads])
    return (
        [t for t, m in zip(texts, te, strict=True) if not m], y[~te],
        [t for t, m in zip(texts, te, strict=True) if m], y[te],
    )


def scores(y_true, pred) -> dict:
    from sklearn.metrics import (
        accuracy_score,
        balanced_accuracy_score,
        f1_score,
        precision_score,
        recall_score,
    )
    return {
        "accuracy": accuracy_score(y_true, pred),
        "balanced_accuracy": balanced_accuracy_score(y_true, pred),
        "precision": precision_score(y_true, pred, zero_division=0),
        "recall": recall_score(y_true, pred, zero_division=0),
        "F1": f1_score(y_true, pred, zero_division=0),
    }


# ─────────────────────────────────────────────────────────────────────────────
# TASK 1 -- the bar
#
# Two TF-IDF variants, because the obvious one is not the fair one. Drug names
# are morphological: -STATIN, -PRAZOLE, -MAB, -OLOL all mark a drug class, and
# word-level TF-IDF cannot see inside a token. Character n-grams can, which
# makes them the honest bar for a transformer to beat -- a transformer's whole
# claim on this corpus is subword handling, so comparing it against a baseline
# that has none would be rigging the fight.
# ─────────────────────────────────────────────────────────────────────────────

def baseline(db: str | None = None) -> dict:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline

    texts, y = corpus(db)
    Xtr, ytr, Xte, yte = split(texts, y)
    out = {"n_train": len(Xtr), "n_test": len(Xte), "brand_rate": float(yte.mean())}

    for name, vec in [
        ("word", TfidfVectorizer(sublinear_tf=True, min_df=2)),
        # char_wb keeps n-grams inside word boundaries, so it learns suffixes
        # rather than spanning the space between two drug names
        ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5),
                                 sublinear_tf=True, min_df=2)),
    ]:
        # class_weight balanced: the corpus is 70% generic and an unweighted fit
        # drifts toward always-generic, which scores 0.70 and decides nothing
        pipe = make_pipeline(vec, LogisticRegression(max_iter=2000, C=4.0,
                                                     class_weight="balanced",
                                                     random_state=SEED))
        pipe.fit(Xtr, ytr)
        out[name] = scores(yte, pipe.predict(Xte))
        out[name]["features"] = len(pipe.steps[0][1].vocabulary_)
        log.info("tfidf-%s: balanced acc %.4f (%d features)",
                 name, out[name]["balanced_accuracy"], out[name]["features"])
    return out


# ─────────────────────────────────────────────────────────────────────────────
# TASK 3 -- semantic search against the search we actually ship
#
# The keyword baseline is not a strawman written for this comparison: it is the
# search `ask -> find a drug by description` falls back to without the [nlp]
# extra. Prefix match outranks every-term-present. It began as a port of the
# buying desk's JavaScript search; with the desk deleted this is the only copy,
# which is one algorithm in one language instead of two.
#
# No vector store. 6,359 descriptions x 384 dimensions is a 9.8MB float32
# matrix; the whole "index" is one np.dot and it is EXACT, where a FAISS index
# would be approximate and a Chroma install would be 40MB of server to search
# less data than fits in L3 cache. Embeddings are cached to an .npz so a repeat
# run skips the model entirely.
# ─────────────────────────────────────────────────────────────────────────────

EMB_MODEL = "sentence-transformers/all-MiniLM-L6-v2"   # 22M params, 384-dim
EMB_CACHE = "data/desc-embeddings.npz"


def embed(texts: list[str], cache: str = EMB_CACHE) -> np.ndarray:
    from pathlib import Path

    p = Path(cache)
    if p.exists():
        z = np.load(p, allow_pickle=True)
        if list(z["texts"]) == texts:          # same corpus, reuse
            log.info("embeddings from cache (%s)", cache)
            return z["vecs"]
        log.info("corpus changed, re-embedding")

    from sentence_transformers import SentenceTransformer

    log.info("encoding %d descriptions with %s", len(texts), EMB_MODEL)
    vecs = SentenceTransformer(EMB_MODEL).encode(
        texts, normalize_embeddings=True, batch_size=64,
        show_progress_bar=False).astype(np.float32)
    p.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(p, texts=np.array(texts, dtype=object), vecs=vecs)
    return vecs


def semantic_search(texts, vecs, model, q: str, k: int = 5) -> list[tuple[str, float]]:
    qv = model.encode([q], normalize_embeddings=True)[0]
    sim = vecs @ qv                            # normalised, so this IS cosine
    top = np.argpartition(-sim, k)[:k]         # O(n), not a full sort
    return [(texts[i], float(sim[i])) for i in top[np.argsort(-sim[top])]]


def search(db: str | None = None, queries: list[str] | None = None) -> dict:
    from sentence_transformers import SentenceTransformer

    texts, _ = corpus(db)
    vecs = embed(texts)
    model = SentenceTransformer(EMB_MODEL)
    qs = queries or [
        "cholesterol medication",     # no drug in the corpus contains "cholesterol"
        "blood thinner",
        "drug for diabetes",
        "inhaler for asthma",
        "atorvastatin 40",            # keyword should own this one
        "ZEPBOUND",                   # and this
    ]
    out = {}
    for q in qs:
        out[q] = {"keyword": keyword_search(texts, q),
                  "semantic": semantic_search(texts, vecs, model, q)}
    return {"n": len(texts), "results": out}


def _quiet_hub() -> None:
    for name in ("httpx", "huggingface_hub", "transformers", "sentence_transformers"):
        logging.getLogger(name).setLevel(logging.WARNING)


# ─────────────────────────────────────────────────────────────────────────────
# TASK 2 -- fine-tune a transformer against that bar
#
# Hand-rolled training loop rather than transformers' Trainer: Trainer drags in
# accelerate and datasets, and the loop is the same twenty lines already written
# in nn.py -- shuffle, batch, step, early-stop on validation.
#
# The honest question is not "does it work" but "does it beat char n-grams,
# which cost nothing and train in two seconds".
# ─────────────────────────────────────────────────────────────────────────────

FT_MODEL = "distilbert-base-uncased"      # 66M params, ~250MB, CPU-feasible


def finetune(db: str | None = None, epochs: int = 3, batch: int = 32,
             lr: float = 3e-5, max_len: int = 32, model_name: str = FT_MODEL) -> dict:
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    _quiet_hub()
    torch.manual_seed(SEED)
    texts, y = corpus(db)
    Xtr, ytr, Xte, yte = split(texts, y)
    # a slice of train held back for early stopping, so test stays untouched
    cut = int(len(Xtr) * 0.85)
    Xva, yva, Xtr, ytr = Xtr[cut:], ytr[cut:], Xtr[:cut], ytr[:cut]

    tok = AutoTokenizer.from_pretrained(model_name)
    net = AutoModelForSequenceClassification.from_pretrained(model_name, num_labels=2)

    def batches(X, Y, size, shuffle=False):
        idx = np.random.permutation(len(X)) if shuffle else np.arange(len(X))
        for i in range(0, len(idx), size):
            j = idx[i:i + size]
            # max_len 32: the longest description is well under that, and a
            # padded 512 would be 16x the compute for the same tokens
            enc = tok([X[t] for t in j], truncation=True, max_length=max_len,
                      padding=True, return_tensors="pt")
            yield enc, torch.tensor(Y[j])

    # the corpus is 70% generic; unweighted, the model drifts to always-generic
    w = torch.tensor([1.0, float((ytr == 0).sum() / max((ytr == 1).sum(), 1))])
    lossfn = torch.nn.CrossEntropyLoss(weight=w)
    opt = torch.optim.AdamW(net.parameters(), lr=lr)

    best, best_state, history = -1.0, None, []
    for ep in range(epochs):
        net.train()
        total = 0.0
        for enc, yb in batches(Xtr, ytr, batch, shuffle=True):
            opt.zero_grad()
            loss = lossfn(net(**enc).logits, yb)
            loss.backward()
            opt.step()
            total += loss.item() * len(yb)
        net.eval()
        with torch.no_grad():
            pv = np.concatenate([net(**e).logits.argmax(1).numpy()
                                 for e, _ in batches(Xva, yva, 64)])
        m = scores(yva, pv)
        history.append({"epoch": ep, "train_loss": total / len(Xtr), **m})
        log.info("epoch %d  loss %.4f  val balanced acc %.4f",
                 ep, total / len(Xtr), m["balanced_accuracy"])
        if m["balanced_accuracy"] > best:
            best = m["balanced_accuracy"]
            best_state = {k: v.clone() for k, v in net.state_dict().items()}

    net.load_state_dict(best_state)
    net.eval()
    with torch.no_grad():
        pt = np.concatenate([net(**e).logits.argmax(1).numpy()
                             for e, _ in batches(Xte, yte, 64)])
    return {"model": model_name, "best_val": best, "test": scores(yte, pt),
            "history": history, "n_train": len(Xtr), "n_test": len(Xte),
            "params": sum(p.numel() for p in net.parameters())}
