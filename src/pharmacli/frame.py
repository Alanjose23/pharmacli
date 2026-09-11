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

# gini for every ndc movement, 0 = ndc moves price equally, 1 does it all
def gini(values) -> float:
    x = np.sort(np.asarray(values, dtype=float))
    n = len(x)
    if n == 0 or x.sum() == 0:
        return 0.0
    # sorts whole array
    return float((2 * np.arange(1, n + 1) - n - 1).dot(x) / (n * x.sum()))

# ── how big a move is, per rate class ────────────────────────────────────────
# This is the DERIVATION behind cli.MOVE_EV and cli.DOWN_SHARE. Those constants
# are pasted into cli.py so the CLI needs no panel to run, but they came from
# here and this function is how you re-check them:
#
#     from pharmacli.frame import load, move_profile
#     print(move_profile(load("pharmacli.db")))
#
# The old code used a single 1.74% for every drug and assumed generics fall and
# brands rise. Measured over 227,638 real moves that is wrong on both counts:
# brand up-moves and down-moves very nearly cancel, so a brand's EXPECTED change
# is ~+0.005%, not +1.74% -- an overstatement of about 362x.
def move_profile(df: pd.DataFrame) -> pd.DataFrame:
    d = weekly_change(df)
    # a move is a week where the price actually changed; unchanged weeks and
    # weeks with no comparable prior observation carry no directional signal
    moved = d[d["wow"].notna() & (d["wow"] != 0)]

    rows = {}
    for rc, s in moved.groupby("rate_class", observed=True):
        dn, up = s.loc[s["wow"] < 0, "wow"], s.loc[s["wow"] > 0, "wow"]
        if not len(s):
            continue
        share = len(dn) / len(s)
        # an all-one-direction class would make the other median NaN; 0.0 is the
        # right stand-in, since a direction that never happens contributes nothing
        med_dn = float(dn.abs().median()) if len(dn) else 0.0
        med_up = float(up.abs().median()) if len(up) else 0.0
        rows[rc] = {
            "n": len(s),
            "down_share": share,
            "med_down": med_dn,
            "med_up": med_up,
            "ev": -share * med_dn + (1 - share) * med_up,
        }
    return pd.DataFrame(rows).T
