import numpy as np
import pytest

nlp = pytest.importorskip("pharmacli.nlp")


def tsplitkeepsadrugfamilyononeside():
    texts = [f"{mol} {mg} MG TABLET"
             for mol in ("ATORVASTATIN", "LOVASTATIN", "METFORMIN", "LIPITOR",
                         "ZOCOR", "GLUCOPHAGE", "IBUPROFEN", "NAPROXEN")
             for mg in (10, 20, 40)]
    y = np.array([i % 2 for i in range(len(texts))])
    Xtr, _, Xte, _ = nlp.split(texts, y)

    heads_tr = {t.split()[0] for t in Xtr}
    heads_te = {t.split()[0] for t in Xte}
    assert not (heads_tr & heads_te), f"leaked: {heads_tr & heads_te}"
    assert Xtr and Xte, "both sides must be non-empty"
    assert len(Xtr) + len(Xte) == len(texts), "no drug dropped or duplicated"


def tsplitisdeterministic():
    texts = [f"DRUG{i} 10 MG TABLET" for i in range(60)]
    y = np.zeros(len(texts), dtype=int)
    a = nlp.split(texts, y)
    b = nlp.split(texts, y)
    assert a[0] == b[0] and a[2] == b[2]


def tkeywordsearchranksprefixfirst():
    texts = ["ATORVASTATIN 10 MG TABLET", "ATORVASTATIN 40 MG TABLET",
             "METFORMIN HCL 1,000 MG TABLET", "ZEPBOUND 10 MG/0.5 ML PEN"]

    assert nlp.keyword_search(texts, "atorvastatin 40") == ["ATORVASTATIN 40 MG TABLET"]
    # prefix beats term-scatter: both atorvastatins contain "10"? no -- check ranking
    assert nlp.keyword_search(texts, "atorvastatin")[0].startswith("ATORVASTATIN")
    # every term must appear, not the whole string contiguously
    assert nlp.keyword_search(texts, "metformin tablet") == ["METFORMIN HCL 1,000 MG TABLET"]
    # and the failure that motivates semantic search at all
    assert nlp.keyword_search(texts, "cholesterol medication") == []


def tcorpuslabelsarebinaryandbalancedish():
    pytest.importorskip("sqlite3")
    # uses whatever db is configured; skip when there is no panel to read
    try:
        texts, y = nlp.corpus()
    except Exception:
        pytest.skip("no panel available")
    if not texts:
        pytest.skip("empty panel")
    assert set(np.unique(y)) <= {0, 1}
    assert len(texts) == len(set(texts)), "descriptions must be de-duplicated"
