

from pharmacli.cli import (expected_price, move_odds, days_to_reprice,reorder_advice)


def tmoveoddscomefromthepanel():

    assert move_odds(7) == 0.002          # just repriced
    assert move_odds(18) == 0.613         # entering the window
    assert move_odds(24) == 0.960         # peak
    assert move_odds(400) == 0.045        # off-cycle drugs, own rhythm
    assert days_to_reprice(24) == 0       # due now
    assert days_to_reprice(10) == 14


def tgenericsfallbrandsrise():

    at_peak = 24
    assert expected_price(1.0, at_peak, "G") < 1.0        # generic erodes
    assert expected_price(1.0, at_peak, "B") > 1.0        # brand rises
    assert expected_price(1.0, at_peak, "B-BIO") > 1.0    # biologic counts as brand
    # a price that just repriced barely moves in expectation, either way
    assert abs(expected_price(1.0, 7, "G") - 1.0) < 0.001


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
