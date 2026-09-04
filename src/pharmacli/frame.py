# nadac panel pandas

import sqlite3
from contextlib import closing

import numpy as np
import pandas as pd

from pharmacli.config import load as _config


# B = brand, B-ANDA = auth. generic, B-BIO = biologic
BRAND = ("B", "B-ANDA", "B-BIO")

# columns that are repeated for all rows
CATEGORICAL = (
    "ndc",
    "description",
    "pricing_unit",
    "rate_class",
    "otc",
    "explanation_code",
)

DATES = ("effective_date", "as_of_date", "generic_effective_date")

# prices stay
PRICES = ("unit_price", "generic_price")

# one week value
WEEK = pd.Timedelta("7D")

#read nadac table to an optimized fram, sorted by ndc and as of date
def load(db: str | sqlite3.Connection | None = None) -> pd.DataFrame:
    """Read the nadac table into a typed, memory-optimized frame.

    Sorted by (ndc, as_of_date), which is what makes weekly_change correct.
    """
    # closed connection, as ut would leak a file handle per call
    if isinstance(db, sqlite3.Connection):
        df = pd.read_sql("SELECT * FROM nadac", db)
    else:
        with closing(sqlite3.connect(db or _config().db)) as conn:
            df = pd.read_sql("SELECT * FROM nadac", conn)

    # everything arrives as TEXT
    for col in DATES:
        df[col] = pd.to_datetime(df[col], errors="coerce")
    for col in PRICES:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in CATEGORICAL:
        if col in df.columns:
            df[col] = df[col].astype("category")

    return df.sort_values(["ndc", "as_of_date"], ignore_index=True)

# weekly change function per NDC, not applicable in some instances, such as no previous observation and over a week change
def weekly_change(df: pd.DataFrame) -> pd.DataFrame:
  
    # re-sort
    out = df.sort_values(["ndc", "as_of_date"], ignore_index=True)
    grouped = out.groupby("ndc", observed=True)

    # percentage change
    out["wow"] = grouped["unit_price"].pct_change(fill_method=None)

    # fixes possible leaks if weeks dont add up to one 1 week, aka week 3 vs week 5 data
    out.loc[grouped["as_of_date"].diff() != WEEK, "wow"] = np.nan
    return out

# brand rows carrying gen. price with ratio
def brand_generic(df: pd.DataFrame) -> pd.DataFrame:
    # B and B-ANDA
    out = df[
        df["rate_class"].isin(BRAND)
        & df["generic_price"].notna()
        & (df["generic_price"] > 0)
    ].copy()
    out["ratio"] = out["unit_price"] / out["generic_price"]
    return out

#last seen ndc in database
def last_seen(df: pd.DataFrame) -> pd.Series:
    return df.groupby("ndc", observed=True)["as_of_date"].max()

# gini for every ndc movement, 0 = ndc moves price equally, 1 does it all
def gini(values) -> float:
    x = np.sort(np.asarray(values, dtype=float))
    n = len(x)
    if n == 0 or x.sum() == 0:
        return 0.0
    # sorts whole array
    return float((2 * np.arange(1, n + 1) - n - 1).dot(x) / (n * x.sum()))

## percentage analysis because change in prices is not able to be directly analyzed.
def bootstrap_ci(values, n: int = 2000, seed: int = 0) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    v = np.asarray(values, dtype=float)
    # median
    draws = np.median(rng.choice(v, (n, len(v)), replace=True), axis=1)
    lo, hi = np.percentile(draws, [2.5, 97.5])
    return float(lo), float(hi)
