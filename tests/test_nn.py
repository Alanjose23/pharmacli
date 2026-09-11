import numpy as np
import pytest

torch = pytest.importorskip("torch")
nn = pytest.importorskip("pharmacli.nn")


def tbackpropmatchesautograd():
    r = nn.toy(steps=50)
    for name, diff in r["grad_max_abs_diff"].items():
        assert diff < 1e-9, f"d{name} disagrees with autograd by {diff:.3e}"
    # and they stay together through 50 SGD steps, not just at initialisation
    assert r["loss_diff"] < 1e-9
    assert r["weight_max_abs_diff"] < 1e-9


def tthetoyproblemisnotlinearlyseparable():
    from sklearn.linear_model import LogisticRegression

    rng = np.random.default_rng(0)
    x = rng.normal(size=(400, 2))
    y = ((x[:, 0] > 0) ^ (x[:, 1] > 0)).astype(int)
    # a linear model on the raw XOR features is at chance
    assert LogisticRegression().fit(x, y).score(x, y) < 0.65


def tforwardpassmatchestorch():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(16, 4))
    p = nn._init(4, np.random.default_rng(0))
    prob, _ = nn.forward_numpy(p, x)

    t = {k: torch.tensor(v, dtype=torch.float64) for k, v in p.items()}
    z2 = torch.tanh(torch.tensor(x) @ t["W1"] + t["b1"]) @ t["W2"] + t["b2"]
    assert np.abs(prob - torch.sigmoid(z2).numpy()).max() < 1e-12


def tbceclipsinsteadofreturninginf():
    # log(0) is -inf and one such row poisons the whole epoch's mean
    assert np.isfinite(nn.bce(np.array([[0.0], [1.0]]), np.array([1.0, 0.0])))


def tmlplayershaveregularisation():
    import torch.nn as tnn

    net = nn._mlp(10, (8, 4), dropout=0.25)
    kinds = [type(m) for m in net]
    # batch norm BEFORE the activation, dropout AFTER it -- the other orders
    # normalise a distribution the next layer never sees
    assert kinds.index(tnn.BatchNorm1d) < kinds.index(tnn.ReLU) < kinds.index(tnn.Dropout)
    assert isinstance(net[-1], tnn.Linear)     # logits out, loss applies sigmoid


def taugmentationpreserveslabelmeaning():
    rng = np.random.default_rng(0)
    batch = np.zeros((8, 28, 28), dtype=np.float32)
    batch[:, :4, :] = 1.0                      # a bright band along the TOP
    out = nn._augment(batch, rng)
    # after horizontal flips and <=2px shifts the band is still near the top
    assert out[:, :8, :].sum() > out[:, 8:, :].sum()
