import logging

import numpy as np

# Same seed as the sklearn side, for the same reason: a tracked run has to be
# replayable. See model.SEED.
from pharmacli.model import SEED

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# TASK 1 -- backprop by hand, checked against autograd
#
# The point is not that the net works. The point is that the four gradients
# below were DERIVED, and that autograd independently produces the same
# numbers. If you cannot write these, you are trusting a library you cannot
# check.
#
# Network:  x -> [W1,b1] -> tanh -> [W2,b2] -> sigmoid -> binary cross-entropy
#
#   z1 = x @ W1 + b1          a1 = tanh(z1)
#   z2 = a1 @ W2 + b2         p  = sigmoid(z2)
#   L  = -mean( y*log(p) + (1-y)*log(1-p) )
#
# The chain rule, term by term:
#
#   dL/dz2 = (p - y) / N          <- sigmoid and BCE collapse into this. The
#                                    sigmoid derivative p(1-p) cancels against
#                                    the 1/(p(1-p)) from BCE. That cancellation
#                                    is why this pairing is numerically stable
#                                    and why torch ships BCEWithLogitsLoss.
#   dL/dW2 = a1.T @ dz2
#   dL/db2 = sum(dz2, axis=0)
#   dL/da1 = dz2 @ W2.T
#   dL/dz1 = dL/da1 * (1 - tanh(z1)^2)     <- tanh' = 1 - tanh^2
#   dL/dW1 = x.T @ dz1
#   dL/db1 = sum(dz1, axis=0)
# ─────────────────────────────────────────────────────────────────────────────

HIDDEN = 8


def _init(n_features: int, rng) -> dict:
    return {
        "W1": rng.normal(0, np.sqrt(2.0 / n_features), (n_features, HIDDEN)),
        "b1": np.zeros(HIDDEN),
        "W2": rng.normal(0, np.sqrt(2.0 / HIDDEN), (HIDDEN, 1)),
        "b2": np.zeros(1),
    }


def forward_numpy(p: dict, x: np.ndarray) -> tuple[np.ndarray, dict]:
    z1 = x @ p["W1"] + p["b1"]
    a1 = np.tanh(z1)
    z2 = a1 @ p["W2"] + p["b2"]
    # clip before exp: a large negative z2 overflows exp() and yields inf, and
    # the loss becomes nan several steps before anyone notices
    prob = 1.0 / (1.0 + np.exp(-np.clip(z2, -50, 50)))
    return prob, {"x": x, "z1": z1, "a1": a1, "prob": prob}


def backward_numpy(p: dict, cache: dict, y: np.ndarray) -> dict:
    n = len(y)
    dz2 = (cache["prob"] - y.reshape(-1, 1)) / n          # sigmoid+BCE collapse
    grads = {
        "W2": cache["a1"].T @ dz2,
        "b2": dz2.sum(axis=0),
    }
    da1 = dz2 @ p["W2"].T
    dz1 = da1 * (1.0 - np.tanh(cache["z1"]) ** 2)          # tanh' = 1 - tanh^2
    grads["W1"] = cache["x"].T @ dz1
    grads["b1"] = dz1.sum(axis=0)
    return grads


def bce(prob: np.ndarray, y: np.ndarray) -> float:
    eps = 1e-12                       # log(0) is -inf; the loss curve dies there
    p = np.clip(prob.ravel(), eps, 1 - eps)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def toy(n: int = 256, steps: int = 200, lr: float = 0.5) -> dict:
    import torch

    rng = np.random.default_rng(SEED)
    # XOR-ish: not linearly separable, so the hidden layer has to do work. A
    # linearly separable toy would agree even with a broken hidden-layer
    # gradient, which is exactly the bug this is meant to catch.
    x = rng.normal(size=(n, 2))
    y = ((x[:, 0] > 0) ^ (x[:, 1] > 0)).astype(float)
    x = np.hstack([x, x[:, :1] * x[:, 1:2], np.ones((n, 1))])   # 4 features

    p_np = _init(x.shape[1], np.random.default_rng(SEED))
    t = {k: torch.tensor(v, dtype=torch.float64, requires_grad=True)
         for k, v in _init(x.shape[1], np.random.default_rng(SEED)).items()}
    xt = torch.tensor(x, dtype=torch.float64)
    yt = torch.tensor(y, dtype=torch.float64).reshape(-1, 1)

    # one definition, two call sites. Written out twice, a later edit to the
    # architecture could touch the training copy and not the scoring copy, and
    # this function would still report agreement -- while comparing two
    # different networks.
    def logits():
        return torch.tanh(xt @ t["W1"] + t["b1"]) @ t["W2"] + t["b2"]

    first = {}
    for step in range(steps):
        prob, cache = forward_numpy(p_np, x)
        g_np = backward_numpy(p_np, cache, y)

        # autograd, same maths, no derivation
        loss_t = torch.nn.functional.binary_cross_entropy_with_logits(logits(), yt)
        for v in t.values():
            v.grad = None
        loss_t.backward()

        if step == 0:
            # the reconciliation that matters: BEFORE either has taken a step,
            # so it compares gradients rather than accumulated drift
            first = {k: (g_np[k], t[k].grad.numpy()) for k in g_np}

        for k in p_np:                                  # plain SGD, both sides
            p_np[k] -= lr * g_np[k]
            with torch.no_grad():
                t[k] -= lr * t[k].grad

    loss_np = bce(forward_numpy(p_np, x)[0], y)
    with torch.no_grad():
        loss_pt = float(torch.nn.functional.binary_cross_entropy_with_logits(logits(), yt))

    return {
        "grad_max_abs_diff": {k: float(np.abs(a - b).max()) for k, (a, b) in first.items()},
        "loss_numpy": loss_np,
        "loss_torch": loss_pt,
        "loss_diff": abs(loss_np - loss_pt),
        "weight_max_abs_diff": max(
            float(np.abs(p_np[k] - t[k].detach().numpy()).max()) for k in p_np),
        "steps": steps,
    }


# ─────────────────────────────────────────────────────────────────────────────
# TASK 2 -- a feed-forward net on the real NADAC panel
#
# This is the only task that touches pharmacy data, and it exists to be
# COMPARED, not deployed. It reuses model.features(), track.splits() and
# model.scores() so the number it produces sits on the same axis as the
# HistGradientBoosting champion and the three baselines already in MLflow.
#
# The expected result is that it loses. Gradient boosting beats neural nets on
# small heterogeneous tabular data as a rule -- there is no spatial or
# sequential structure here for a net to exploit, and 8 numeric + 3 categorical
# columns is not where depth pays. Reporting that honestly is the deliverable.
# ─────────────────────────────────────────────────────────────────────────────

def _mlp(n_in: int, hidden: tuple[int, ...], dropout: float):
    import torch.nn as tnn

    layers, prev = [], n_in
    for h in hidden:
        layers += [tnn.Linear(prev, h), tnn.BatchNorm1d(h), tnn.ReLU(), tnn.Dropout(dropout)]
        prev = h
    layers.append(tnn.Linear(prev, 1))          # logits; the loss applies sigmoid
    return tnn.Sequential(*layers)


def tabular(db: str | None = None, sample: int | None = None, epochs: int = 30,
            hidden: tuple[int, ...] = (64, 32), dropout: float = 0.3,
            lr: float = 1e-3, batch: int = 4096, patience: int = 5,
            logdir: str = "runs/tabular") -> dict:
    import mlflow
    import torch
    import torch.nn as tnn
    from torch.utils.tensorboard import SummaryWriter

    from pharmacli import model, track
    from pharmacli.frame import load as load_frame

    torch.manual_seed(SEED)
    np.random.seed(SEED)

    log.info("building the panel")
    df = load_frame(db)
    if sample:
        df = df[df["ndc"].isin(df["ndc"].unique()[:sample])]
    d = model.features(df)
    (Xtr, ytr), (Xva, yva), (Xte, yte) = track.splits(d)

    # The SAME preprocessor the champion uses -- median/most-frequent impute,
    # StandardScaler, OneHotEncoder(min_frequency=20). Reusing it is what makes
    # this a comparison of MODELS rather than of preprocessing. Fit on train
    # only; the split is temporal, so touching val/test here would leak.
    pre = model.pipeline().named_steps["pre"]
    A = np.asarray(pre.fit_transform(Xtr), dtype=np.float32)
    B = np.asarray(pre.transform(Xva), dtype=np.float32)
    C = np.asarray(pre.transform(Xte), dtype=np.float32)
    ya = torch.tensor(ytr.to_numpy(), dtype=torch.float32).reshape(-1, 1)

    # ~4% of rows are positive. Without this the net minimises loss by predicting
    # "never moves" for everything, which scores 96% accuracy and is useless --
    # the same trap model.scores() refuses to report accuracy for.
    pos = float(ya.mean())
    pos_weight = torch.tensor([(1 - pos) / max(pos, 1e-9)])
    lossfn = tnn.BCEWithLogitsLoss(pos_weight=pos_weight)

    net = _mlp(A.shape[1], hidden, dropout)
    opt = torch.optim.AdamW(net.parameters(), lr=lr)
    # halve the LR when validation PR-AUC stops improving; the alternative is
    # bouncing around a minimum the fixed rate is too coarse to enter
    sched = torch.optim.lr_scheduler.ReduceLROnPlateau(opt, mode="max", factor=0.5, patience=2)

    At = torch.from_numpy(A)
    writer = SummaryWriter(logdir)
    best, best_state, waited, history = -1.0, None, 0, []

    for epoch in range(epochs):
        net.train()
        # a fresh permutation per epoch: fixed batch composition lets BatchNorm
        # memorise per-batch statistics instead of learning the distribution
        order = torch.randperm(len(At))
        total = 0.0
        for i in range(0, len(order), batch):
            idx = order[i:i + batch]
            if len(idx) < 2:
                continue                    # BatchNorm needs >1 row to have a variance
            opt.zero_grad()
            out = lossfn(net(At[idx]), ya[idx])
            out.backward()
            opt.step()
            total += out.item() * len(idx)   # .item() detaches; float() on a
            #                                  grad-tracking tensor warns
        train_loss = total / len(order)

        net.eval()
        with torch.no_grad():
            pv = torch.sigmoid(net(torch.from_numpy(B))).numpy().ravel()
        m = model.scores(yva, pv)
        sched.step(m["PR-AUC"])
        history.append({"epoch": epoch, "train_loss": train_loss, **m})
        writer.add_scalar("loss/train", train_loss, epoch)
        writer.add_scalar("val/PR-AUC", m["PR-AUC"], epoch)
        writer.add_scalar("val/ROC-AUC", m["ROC-AUC"], epoch)
        writer.add_scalar("lr", opt.param_groups[0]["lr"], epoch)
        log.info("epoch %2d  loss %.4f  val PR-AUC %.4f", epoch, train_loss, m["PR-AUC"])

        # early stopping on validation PR-AUC, not on loss: loss is dominated by
        # the 96% negative class and keeps falling while ranking gets worse
        if m["PR-AUC"] > best:
            best, waited = m["PR-AUC"], 0
            best_state = {k: v.clone() for k, v in net.state_dict().items()}
        else:
            waited += 1
            if waited >= patience:
                log.info("early stop: %d epochs without a better val PR-AUC", patience)
                break

    net.load_state_dict(best_state)             # roll back to the best epoch
    net.eval()
    with torch.no_grad():                       # test touched ONCE, at the end
        pt = torch.sigmoid(net(torch.from_numpy(C))).numpy().ravel()
    test = model.scores(yte, pt)
    writer.close()

    # Same experiment as the sweep, so `mlflow ui` ranks this against the
    # champion and the baselines on one screen instead of in two places.
    mlflow.set_tracking_uri(track.TRACKING_URI)
    mlflow.set_experiment("pharmacli-mover")
    # a --sample run is a smoke test, not a result. Name it so it cannot be
    # mistaken for the real one in the MLflow comparison.
    run_name = f"nn-tabular-sample{sample}" if sample else "nn-tabular"
    with mlflow.start_run(run_name=run_name):
        mlflow.log_params({"est": "mlp", "hidden": str(hidden), "dropout": dropout,
                           "lr": lr, "batch": batch, "seed": SEED, "sample": sample or 0,
                           "epochs_run": len(history), "pos_weight": float(pos_weight)})
        mlflow.log_metrics({f"val_{k}": float(v) for k, v in
                            max(history, key=lambda h: h["PR-AUC"]).items() if k != "epoch"})
        mlflow.log_metrics({f"test_{k}": float(v) for k, v in test.items()})

    return {"best_val_pr_auc": best, "test": test, "history": history,
            "n_features": A.shape[1], "logdir": logdir}


# ─────────────────────────────────────────────────────────────────────────────
# TASK 3 -- a CNN on medical images
#
# A LABELLED EXERCISE, not a pharmacy feature. pharmacli prices drugs; it has
# no image data and no clinical role. This exists because the week asks for a
# CNN on a real image task, and MedMNIST is free, small enough for CPU, and
# properly documented.
#
# Nothing here is a diagnostic tool and nothing here should ever be pointed at
# a patient.
#
# No torchvision and no medmnist package: the dataset is a plain .npz behind a
# URL, and the augmentations below are four lines of numpy. Skipping both saves
# ~200MB and keeps the transformations visible, which is the point of a week
# spent not treating this as a black box.
# ─────────────────────────────────────────────────────────────────────────────

# MedMNIST's canonical home is Zenodo (record 10519652). This points at the
# HuggingFace mirror instead, which carries the byte-identical .npz files,
# because zenodo.org is unreachable from this machine -- every request,
# including the site root, times out at the connection. Swap the URL back if
# your network can see Zenodo; the archive format is the same.
MEDMNIST_URL = ("https://huggingface.co/datasets/albertvillanova/medmnist-v2"
                "/resolve/main/data/{}.npz")


def _fetch_npz(name: str, cache_dir: str = "data") -> dict:
    from pathlib import Path
    from urllib.request import urlopen

    path = Path(cache_dir) / f"{name}.npz"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        url = MEDMNIST_URL.format(name)
        log.info("downloading %s", url)
        with urlopen(url, timeout=120) as r, open(path, "wb") as fh:
            fh.write(r.read())
    return dict(np.load(path))


def _augment(batch: np.ndarray, rng) -> np.ndarray:
    out = batch.copy()
    flip = rng.random(len(out)) < 0.5
    out[flip] = out[flip, :, ::-1]
    for i in range(len(out)):
        dx, dy = rng.integers(-2, 3, size=2)
        out[i] = np.roll(out[i], (dy, dx), axis=(0, 1))
    return out


def _cnn(n_classes: int):
    import torch.nn as tnn

    # Two conv blocks is enough for 28x28. A deeper stack would pool the image
    # away before the classifier sees anything.
    return tnn.Sequential(
        tnn.Conv2d(1, 16, 3, padding=1), tnn.BatchNorm2d(16), tnn.ReLU(),
        tnn.MaxPool2d(2),                                       # 28 -> 14
        tnn.Conv2d(16, 32, 3, padding=1), tnn.BatchNorm2d(32), tnn.ReLU(),
        tnn.MaxPool2d(2),                                       # 14 -> 7
        tnn.Flatten(), tnn.Dropout(0.3), tnn.Linear(32 * 7 * 7, n_classes),
    )


def xray(dataset: str = "pneumoniamnist", epochs: int = 12, batch: int = 128,
         lr: float = 1e-3, augment: bool = True, balance: bool = True,
         seed: int = SEED, logdir: str = "runs/xray") -> dict:
    import torch
    import torch.nn as tnn
    from torch.utils.tensorboard import SummaryWriter

    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    data = _fetch_npz(dataset)

    def prep(split):
        x = data[f"{split}_images"].astype(np.float32) / 255.0
        y = data[f"{split}_labels"].ravel().astype(np.int64)
        return x, y

    xtr, ytr = prep("train")
    xva, yva = prep("val")
    xte, yte = prep("test")
    n_classes = int(max(ytr.max(), yte.max())) + 1
    log.info("%s: train %d, val %d, test %d, %d classes",
             dataset, len(xtr), len(xva), len(xte), n_classes)

    net = _cnn(n_classes)
    opt = torch.optim.AdamW(net.parameters(), lr=lr)

    # PneumoniaMNIST is 74% pneumonia in train AND val, but only 62% in test --
    # val is drawn from the training pool, so it shares train's balance and test
    # does not. Unweighted, the net learns "guess pneumonia" and val accuracy
    # rewards it: ~0.95 on val against ~0.80 balanced accuracy on test. That is
    # distribution shift between val and test, not overfitting, and dropout does
    # not touch it.
    #
    # Inverse-frequency weighting helps, but MEASURE BEFORE BELIEVING IT. Over 3
    # seeds x 6 epochs:
    #
    #   weighting off   balanced acc 0.796 +/- 0.031   normal recall 0.610 +/- 0.069
    #   weighting on    balanced acc 0.824 +/- 0.038   normal recall 0.674 +/- 0.082
    #
    # The mean improves, and the seed-to-seed spread is as large as the effect.
    # A single run of each "showed" weighting making things worse. On a
    # 624-image test set one run is an anecdote.
    counts = np.bincount(ytr, minlength=n_classes).astype(np.float32)
    weight = ((counts.sum() / (n_classes * np.maximum(counts, 1))) if balance
              else np.ones(n_classes, np.float32))
    lossfn = tnn.CrossEntropyLoss(weight=torch.tensor(weight))
    log.info("class counts %s, loss weights %s", counts.astype(int).tolist(),
             np.round(weight, 3).tolist())
    writer = SummaryWriter(logdir)

    def batched(x, y):
        t = torch.from_numpy(x).unsqueeze(1)     # NHW -> NCHW, one channel
        return t, torch.from_numpy(y)

    xva_t, yva_t = batched(xva, yva)
    best, best_state, history = -1.0, None, []

    for epoch in range(epochs):
        net.train()
        order = rng.permutation(len(xtr))
        total = 0.0
        for i in range(0, len(order), batch):
            idx = order[i:i + batch]
            if len(idx) < 2:
                continue
            raw = xtr[idx]
            xb, yb = batched(_augment(raw, rng) if augment else raw, ytr[idx])
            opt.zero_grad()
            out = lossfn(net(xb), yb)
            out.backward()
            opt.step()
            total += out.item() * len(idx)

        net.eval()
        with torch.no_grad():
            va_acc = float((net(xva_t).argmax(1) == yva_t).float().mean())
        history.append({"epoch": epoch, "train_loss": total / len(order), "val_acc": va_acc})
        writer.add_scalar("loss/train", total / len(order), epoch)
        writer.add_scalar("val/accuracy", va_acc, epoch)
        log.info("epoch %2d  loss %.4f  val acc %.4f", epoch, total / len(order), va_acc)
        if va_acc > best:
            best = va_acc
            best_state = {k: v.clone() for k, v in net.state_dict().items()}

    net.load_state_dict(best_state)
    net.eval()
    xte_t, yte_t = batched(xte, yte)
    with torch.no_grad():
        pred = net(xte_t).argmax(1).numpy()
    writer.close()

    # Where it FAILS, per class -- the week asks for this explicitly, and an
    # accuracy number alone hides which direction the errors run.
    confusion = np.zeros((n_classes, n_classes), dtype=int)
    # strict: a pred shorter than yte would fill a confusion matrix that looks
    # fine and counts fewer rows than were scored
    for t, p in zip(yte, pred, strict=True):
        confusion[t, p] += 1
    per_class = {int(c): {"n": int(confusion[c].sum()),
                          "recall": float(confusion[c, c] / max(confusion[c].sum(), 1))}
                 for c in range(n_classes)}
    # Balanced accuracy = mean per-class recall. On a skewed test set plain
    # accuracy rewards guessing the majority; this does not.
    balanced = float(np.mean([v["recall"] for v in per_class.values()]))
    return {"best_val_acc": best, "test_acc": float((pred == yte).mean()),
            "test_balanced_acc": balanced,
            "confusion": confusion.tolist(), "per_class": per_class,
            "history": history, "augmented": augment, "balanced": balance,
            "logdir": logdir}
