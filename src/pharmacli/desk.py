# The buying desk's arithmetic: the repricing calendar, the odds a price moves,
# the expected move per rate class, reorder timing, break-even, and the three
# worklists behind `today`, `check` and `ask`. Split out of cli.py so the
# numbers a pharmacist acts on are not buried among argument parsing.
#
# Nothing here imports cli or parser: the dependency runs one way.

from datetime import date as _date
from datetime import timedelta
from typing import Final

# 8 levels, low to high. Unicode because the alternative is a bar chart that
# needs its own line per week; 34 weeks fit on one line this way.
BLOCKS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


def _sparkline(values: list[float]) -> str:
    lo, hi = min(values), max(values)
    if hi == lo:
        return BLOCKS[len(BLOCKS) // 2] * len(values)
    return "".join(BLOCKS[min(len(BLOCKS) - 1,
                              int((v - lo) / (hi - lo) * len(BLOCKS)))]
                   for v in values)


# ─────────────────────────────────────────────────────────────────────────────
# PRICE CHANGE MATHS
#
# Everything below rests on one measured fact: CMS reprices NADAC on a MONTHLY
# cycle. Across 1,028,250 rows, 99% of consecutive price moves landed exactly 4
# or 5 weeks apart, and the probability that a price moves next week depends
# almost entirely on how old the current price is.
#
# These are OBSERVED rates from the panel, not estimates.
# ─────────────────────────────────────────────────────────────────────────────

# (age of price in days, P(it moves in the coming week)). Read as "up to N days".
MOVE_ODDS: Final[tuple[tuple[int, float], ...]] = (
    (14, 0.002),   # just repriced -- CMS almost never touches it again this soon
    (21, 0.613),   # entering the window
    (28, 0.960),   # THE peak. a price this old almost always moves next week
    (35, 0.002),   # it already moved and reset; this bucket is the far side
    (10**6, 0.045),  # off-cycle drugs -- they do not follow the monthly rhythm
)

# Expected SIGNED change in price, given that a move happens, per rate class.
#
# This replaced a single MEDIAN_MOVE = 0.0174 applied to every drug, plus a rule
# that generics fall and brands rise. Measured over 227,638 real moves in the
# panel, that rule was wrong on both counts:
#
#   class   moves down   E[change | move]   old model    overstated by
#   G          61.4%         -0.681%         -1.740%          2.6x
#   B          53.3%         +0.005%         +1.740%        362x
#   B-ANDA     47.6%         +0.353%         +1.740%          4.9x
#   B-BIO      51.6%         +0.039%         +1.740%         44x
#
# Brands are the headline error. Brand up-moves and down-moves are almost the
# same size and almost equally likely, so they very nearly cancel -- a brand's
# expected weekly change is essentially zero, not a 1.74% rise. On a $346
# specialty brand the old maths showed ~$5.80/unit of expected movement where
# the truth is about half a cent.
#
# One number per class, already probability-weighted, so expected_price needs no
# separate sign rule. Derived by frame.move_profile(); re-run it to re-check.
MOVE_EV: Final[dict[str, float]] = {
    "G":      -0.006806,   # generics erode: more ANDA entrants, lower survey price
    "B":       0.000048,   # brand: up and down cancel. Effectively flat.
    "B-ANDA":  0.003532,   # authorised generic -- the one brand class that moves
    "B-BIO":   0.000393,
}

# How often a move is DOWNWARD, per class. Not used by the maths (MOVE_EV
# already carries it) -- this is shown to the pharmacist, because "generics fall
# 61% of the time" is a fact you can act on and "-0.68%" is not.
DOWN_SHARE: Final[dict[str, float]] = {
    "G": 0.614, "B": 0.533, "B-ANDA": 0.476, "B-BIO": 0.516,
}

# Midpoint of the 22-28 day repricing peak. Used to answer "how many days until
# this price is likely to change".
CYCLE_PEAK: Final[int] = 24


# ─────────────────────────────────────────────────────────────────────────────
# THE REPRICING CALENDAR
#
# 8 of 33 files in the panel carry 11.7-17.9% of NDCs moving >=5%; the other 25
# carry 0.00-0.45%. So "will prices move next week" is not a forecasting problem
# -- it is a date, and CMS publishes it in advance: the NADAC Monthly file lands
# on the first Monday on or after the 15th, and the weekly file carrying it is
# dated that Wednesday.
#
# That rule reproduces ALL 8 observed repricing files in the panel exactly, with
# no fitting and no model. What the trained model is genuinely for is the
# remaining question -- WHICH drugs move inside that file -- where it lifts
# precision from the calendar's 0.151 to 0.417.
# ─────────────────────────────────────────────────────────────────────────────

def _monthly_file(year: int, month: int) -> _date:
    d = _date(year, month, 15)
    d += timedelta(days=(7 - d.weekday()) % 7)   # first Monday on or after the 15th
    return d + timedelta(days=2)                 # the Wednesday file that carries it


def next_reprice(after: _date) -> _date:
    d = _monthly_file(after.year, after.month)
    if d <= after:                               # this month's has already landed
        y, m = (after.year + 1, 1) if after.month == 12 else (after.year, after.month + 1)
        d = _monthly_file(y, m)
    return d


def move_odds(age_days: float) -> float:
    for limit, odds in MOVE_ODDS:
        if age_days <= limit:
            return odds
    return MOVE_ODDS[-1][1]


def expected_price(price: float, age_days: float, rate_class: str) -> float:
    # an unrecognised class is treated as plain brand: the most conservative
    # option, since B is the flattest and will not invent movement that is not there
    ev = MOVE_EV.get(rate_class, MOVE_EV["B"])
    return price * (1 + ev * move_odds(age_days))


def days_to_reprice(age_days: float) -> float:
    return max(0.0, CYCLE_PEAK - age_days)


# how long the wholesaler takes. Most community pharmacies get next-day
# delivery on a daily order cycle; two days is the safe planning assumption.
LEAD_DAYS: Final[int] = 2

# Smallest dollar swing worth putting in front of a pharmacist as a REASON to
# move an order. Measured on 2026-07-08, a peak repricing week where 4,808 of
# 4,918 shelf positions had a likely move: median swing $0.09, p95 $0.69, max
# $4.08, and $880 across the whole shelf.
#
# The dispensing fee ($10.64) was the obvious candidate and is wrong -- it
# silences every row, even on a peak week. It would be the right bar if acting
# cost something, but re-timing an order you are placing anyway costs nothing.
# So the bar is only "loud enough not to be noise". A dollar admits ~109
# positions on a peak week and rejects the $0.26 advice that reading a raw
# swing produces.
#
# The honest headline is that per-drug timing is worth CENTS; the money is the
# aggregate, which the worklist already totals.
MATERIAL_SWING: Final[float] = 1.00


def reorder_advice(on_hand: float, monthly: float, age_days: float,
                   price: float, rate_class: str,
                   lead_days: int = LEAD_DAYS, min_swing: float = 0.0) -> dict:
    daily = monthly / 30.0 if monthly else 0.0
    if daily <= 0:
        return {"action": "no data", "why": "no dispensing volume recorded",
                "days_left": None, "order_by": None, "qty": 0, "saving": 0.0}

    days_left = on_hand / daily
    order_by = days_left - lead_days
    to_reprice = days_to_reprice(age_days)
    qty = round(monthly)                       # one month of demand
    projected = expected_price(price, age_days, rate_class)
    swing = (projected - price) * qty          # dollars, over the order quantity

    # A move has to be LIKELY before it is worth bending the order around.
    # Without this gate a drug that just repriced (odds 0.002) would be told to
    # wait 17 days for a saving of nothing -- the schedule says a move is coming
    # eventually, but not soon enough to plan against.
    likely = move_odds(age_days) >= 0.5

    # ...and it has to be worth MONEY, not just likely. Correcting the per-class
    # move sizes shrank brand swings by ~362x, which exposed this gap: without a
    # floor the advice became "order now to capture $0.26" on 1,800 units, a
    # decision no pharmacist should be asked to make. See MATERIAL_SWING for the
    # measurement behind the default.
    material = abs(swing) >= min_swing

    if order_by <= 0:
        action, why, act_on_price = "order now", "at or past stock-out", False
    elif likely and material and swing < 0 and to_reprice < order_by:
        # price expected to fall and there is room on the shelf to wait for it.
        # to_reprice == 0 means the drop lands in THIS week's file -- "wait 0d"
        # reads as a bug, so say what it means: hold, the new price is imminent.
        action = "hold" if to_reprice < 1 else f"wait {to_reprice:.0f}d"
        # WRONG TENSE, fixed: this used to read "drop lands in this week's file".
        # The file on screen IS this week's, so that sentence claimed the drop was
        # already in the price shown -- while still advising a hold, which is a
        # contradiction. move_odds is P(moves in the COMING week), and
        # to_reprice == 0 means the cycle peak has passed, so the move is OVERDUE
        # and lands in the NEXT file. Say that instead.
        why = (f"reprice overdue, {move_odds(age_days):.0%} chance in next week's file"
               if to_reprice < 1 else
               f"generic erosion due in {to_reprice:.0f}d")
        why += f", {order_by:.0f}d of slack"
        act_on_price = True
    elif likely and material and swing > 0 and to_reprice < order_by:
        # price expected to rise before you would otherwise have ordered
        action, why, act_on_price = "order now", f"brand rise likely in {to_reprice:.0f}d", True
    else:
        action = f"in {order_by:.0f}d"
        why = "routine -- no likely move inside the window"
        act_on_price = False

    # saving is only meaningful when the timing was actually changed for price
    return {"action": action, "why": why, "days_left": days_left,
            "order_by": order_by, "qty": qty,
            "saving": abs(swing) if act_on_price else 0.0}


def breakeven(cost: float, nadac: float, fee: float, unit: str) -> str:
    gap = cost - nadac
    if gap <= 0:
        return "never"
    qty = fee / gap
    # under one unit means the gap alone eats the whole fee: every script loses
    # money whatever its size. "0 ML" reads as a rounding bug, so say what it is.
    return f"{qty:,.0f} {unit}" if qty >= 1 else "ANY qty"


# The index. A pharmacist does not ask "when will this price move", they ask
# "what do I do today" -- and the answer is spread across three commands they
# would have to know to run. This ranks the three by MONEY, because that is the
# order they should be worked in, and points at the command that does each one.
#
# Pure sqlite3: no pandas, no model. The two-dependency install gets this.
# The three worklists, as DATA. Split out of cmdtoday so `ask` answers the same
# three questions from the same arithmetic -- the CLI and the question menu
# cannot drift, because they share one computation and one formatter.
def worklists(db: str, within: int = 14, dead_days: int = 90,
              as_of: str | None = None, lead_days: int = LEAD_DAYS,
              shelf: tuple[dict, dict] | None = None) -> dict:
    from pharmacli.store import connect, costs, last_priced, newest_week, stock

    conn = connect(db)
    try:
        week = as_of or newest_week(conn)
        if week is None:
            return {"error": "no NADAC data. pharmacli fetch"}
        if as_of and not conn.execute(
                "SELECT 1 FROM nadac WHERE as_of_date=? LIMIT 1", (as_of,)).fetchone():
            have = [w for (w,) in conn.execute(
                "SELECT DISTINCT as_of_date FROM nadac"
                " ORDER BY as_of_date DESC LIMIT 6")]
            return {"error": f"no NADAC file for {as_of}. recent: {', '.join(have)}"}
        mine, qty = shelf if shelf else (costs(conn), stock(conn))
        marks = last_priced(conn)
        if not mine:
            return {"error": "no inventory recorded. "
                             "pharmacli watch import data/pharmacy_inventory.csv"}
        holes = ",".join("?" * len(mine))
        rows = conn.execute(
            "SELECT ndc, description, CAST(unit_price AS REAL), pricing_unit,"
            " rate_class, CAST(julianday(as_of_date)-julianday(effective_date) AS INT)"
            f" FROM nadac WHERE as_of_date = ? AND ndc IN ({holes})",
            [week, *mine]).fetchall()
    finally:
        conn.close()

    under, buy, dead = [], [], []
    for ndc, desc, nadac, unit, rc, age in rows:
        oh, mo = qty.get(ndc, (None, None))
        gap = mine[ndc] - nadac
        if gap > 0:
            under.append((gap * (mo or 0), ndc, desc, gap, unit))
        if oh is not None and mo:
            a = reorder_advice(oh, mo, age, mine[ndc], rc, lead_days,
                               min_swing=MATERIAL_SWING)
            if a["order_by"] is not None and a["order_by"] <= within:
                buy.append((a, ndc, desc, unit, age))
            if oh / (mo / 30.0) >= dead_days:
                dead.append((oh * mine[ndc], ndc, desc, oh / (mo / 30.0)))
    # you hold stock in a drug CMS has stopped pricing
    gone = [n for n in mine if marks.get(n) != week and (qty.get(n) or (0,))[0]]
    # ...and NDCs with no row in THIS file at all. For `today` these overlap with
    # `gone`; for a CSV of unknown drugs it is the whole point of the check.
    unpriced = sorted(set(mine) - {r[0] for r in rows})

    # The calendar leads, because on 25 of 33 files the honest answer to "what
    # moves next week" is "nothing, and here is the date when something will".
    nxt = next_reprice(_date.fromisoformat(week))
    return {"week": week, "priced": len(mine), "under": under, "buy": buy,
            "dead": dead, "gone": gone, "unpriced": unpriced,
            "bleed": sum(x[0] for x in under),
            "timing": sum(a["saving"] for a, *_ in buy),
            "capital": sum(x[0] for x in dead),
            "next_reprice": nxt,
            "days_to_reprice": (nxt - _date.fromisoformat(week)).days}


# cmdtoday's print block with `print(` removed. Kept as a list of lines so
# `ask` can reuse the exact wording without re-implementing the formatting --
# if this and worklists() ever disagree, test_ask catches it.
def format_worklists(w: dict, within: int = 14, dead_days: int = 90) -> list[str]:
    dtr = w["days_to_reprice"]
    out = ["", f"{w['week']} NADAC file  ·  {w['priced']:,} shelf positions"]
    if dtr > 7:
        out.append(f"next repricing file {w['next_reprice']} ({dtr} days away)"
                   " -- prices hold until then")
    else:
        out.append(f"next repricing file {w['next_reprice']} ({dtr} days away)"
                   " -- THIS is the week")
    out.append("")

    # APPEAL first: measured at 8x the entire timing play on this inventory.
    if w["under"]:
        out.append(f"APPEAL   {len(w['under']):,} positions cost more than NADAC"
                   f"{'':>8}${w['bleed']:,.2f}/month")
        for _, _ndc, desc, gap, unit in sorted(w["under"], reverse=True)[:3]:
            out.append(f"         {str(desc)[:34]:<35} +${gap:.5f}/{unit}")
        out.append("         pharmacli margin -o appeal.csv")
    else:
        out.append("APPEAL   nothing underwater -- every recorded cost is at or below NADAC")

    out.append("")
    if w["buy"]:
        out.append(f"BUY      {len(w['buy']):,} of {w['priced']:,} need an order"
                   f" inside {within} days{'':>6}${w['timing']:,.2f} timing value")
        for a, _ndc, desc, _, _ in sorted(w["buy"], key=lambda x: x[0]["order_by"])[:3]:
            out.append(f"         {str(desc)[:34]:<35} {a['action']}")
        out.append("         pharmacli reorder")
    else:
        out.append(f"BUY      nothing needs ordering inside {within} days")

    out.append("")
    if w["dead"] or w["gone"]:
        if w["dead"]:
            out.append(f"CLEAR    {len(w['dead']):,} positions over {dead_days}d of supply"
                       f"{'':>10}${w['capital']:,.2f} tied up")
            for _, _ndc, desc, days in sorted(w["dead"], reverse=True)[:3]:
                out.append(f"         {str(desc)[:34]:<35} {days:,.0f} days of supply")
        if w["gone"]:
            out.append(f"         {len(w['gone']):,} stocked NDCs are no longer priced by CMS")
    else:
        # an empty result is an ANSWER. Say which two checks came back clean.
        out.append(f"CLEAR    nothing dead -- every position turns inside {dead_days} days,")
        out.append("         and every stocked NDC is still in the newest file")
    return out
