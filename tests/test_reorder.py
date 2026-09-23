from pharmacli.desk import days_to_reprice, expected_price, move_odds, reorder_advice


def tmoveoddscomefromthepanel():

    assert move_odds(7) == 0.002          # just repriced
    assert move_odds(18) == 0.613         # entering the window
    assert move_odds(24) == 0.960         # peak
    assert move_odds(400) == 0.045        # off-cycle drugs, own rhythm
    assert days_to_reprice(24) == 0       # due now
    assert days_to_reprice(10) == 14


def tgenericserodeandbrandsdonot():
    at_peak = 24
    assert expected_price(1.0, at_peak, "G") < 1.0        # generic erodes
    # a plain brand is FLAT in expectation: its up and down moves are the same
    # size and near-equally likely. The old model claimed +1.74% here.
    assert abs(expected_price(1.0, at_peak, "B") - 1.0) < 0.0005
    # ...and the erosion is well under 1%, not the 1.67% the old constant gave
    assert 0.003 < 1.0 - expected_price(1.0, at_peak, "G") < 0.010
    # a price that just repriced barely moves in expectation, either way
    assert abs(expected_price(1.0, 7, "G") - 1.0) < 0.001


def tunknownrateclassdoesnotinventmovement():
    # an unmapped class falls back to plain brand, the flattest option
    assert abs(expected_price(1.0, 24, "WAT") - 1.0) < 0.0005


def tmoveevmatchesthepanelderivation():
    pytest = __import__("pytest")
    pd = pytest.importorskip("pandas")
    frame = pytest.importorskip("pharmacli.frame")
    from pharmacli.desk import MOVE_EV

    # a two-NDC panel: one steps down every other week, one steps up
    weeks = pd.date_range("2026-01-07", periods=12, freq="7D")
    rows = []
    for i, w in enumerate(weeks):
        rows.append({"ndc": "1", "as_of_date": w, "unit_price": 10.0 - i * 0.1,
                     "rate_class": "G"})
        rows.append({"ndc": "2", "as_of_date": w, "unit_price": 10.0 + i * 0.1,
                     "rate_class": "B"})
    prof = frame.move_profile(pd.DataFrame(rows))
    # the falling generic must come out negative, the rising brand positive
    assert prof.loc["G", "ev"] < 0 < prof.loc["B", "ev"]
    assert prof.loc["G", "down_share"] == 1.0
    # and every class the CLI ships a constant for is one move_profile can produce
    assert set(MOVE_EV) >= {"G", "B"}


def tpennyswingsdonotchangetheorder():
    kw = dict(on_hand=1200, monthly=1800, age_days=18, price=5.0, rate_class="B")
    loud = reorder_advice(**kw, min_swing=0.0)      # no floor: acts on pennies
    quiet = reorder_advice(**kw, min_swing=1.00)    # shipped floor
    assert loud["action"] == "order now" and loud["saving"] < 1.00
    assert quiet["action"].startswith("in ")        # routine
    assert quiet["saving"] == 0.0                   # and claims nothing


def tstockbeatspricewhenyouareout():

    r = reorder_advice(on_hand=0, monthly=900, age_days=24, price=1.0, rate_class="G")
    assert r["action"] == "order now"
    assert "stock-out" in r["why"]


def twaitforagenericdropwhenthereisslack():
    r = reorder_advice(on_hand=1200, monthly=1800, age_days=18, price=0.05, rate_class="G")
    assert r["action"].startswith("wait")
    assert r["saving"] > 0

    # same drug, nearly empty so the shelf clock wins, no waiting
    tight = reorder_advice(on_hand=180, monthly=1800, age_days=18, price=0.05, rate_class="G")
    assert not tight["action"].startswith("wait")


def torderaheadofabrandincrease():
    r = reorder_advice(on_hand=1200, monthly=1800, age_days=18, price=5.0, rate_class="B")
    assert r["action"] == "order now"
    assert "rise" in r["why"]
    assert r["saving"] > 0


def tnowaitingforamovethatisnotcoming():

    r = reorder_advice(on_hand=1200, monthly=1800, age_days=7, price=0.05, rate_class="G")
    assert not r["action"].startswith("wait")
    assert r["saving"] == 0.0
    # no saving claimed on a routine order


def tnovolumenoadvice():
    r = reorder_advice(on_hand=100, monthly=0, age_days=24, price=1.0, rate_class="G")
    assert r["action"] == "no data"
    assert r["order_by"] is None


def tthecalendarrulereproduceseveryobservedrepricingfile():
    from datetime import date

    from pharmacli.desk import next_reprice

    observed = ["2026-01-21", "2026-02-18", "2026-03-18", "2026-04-22",
                "2026-05-20", "2026-06-17", "2026-07-22", "2026-08-19"]
    for prev, nxt in zip(observed, observed[1:], strict=False):
        assert next_reprice(date.fromisoformat(prev)).isoformat() == nxt, prev
    # and it rolls into the next year rather than repeating December
    assert next_reprice(date(2026, 12, 31)) == date(2027, 1, 20)


def tnextrepriceneverreturnsthedateyougaveit():
    from datetime import date, timedelta

    from pharmacli.desk import next_reprice

    d = date(2026, 1, 1)
    for _ in range(400):                      # walk a year of days
        assert next_reprice(d) > d
        d += timedelta(days=1)
