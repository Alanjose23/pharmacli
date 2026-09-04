

import pytest

enrich = pytest.importorskip("pharmacli.enrich")


def tpadnormalisesopenfdatoeleven():
    # openFDA keeps the original segmentation; NADAC pads every segment
    assert enrich.pad("0378-3545-01") == "00378354501"   # 4-4-2 labeler
    assert enrich.pad("00378-3545-01") == "00378354501"  # already 5-4-2
    assert enrich.pad("12345-678-9") == "12345067809"    # 5-3-1
    assert len(enrich.pad("0378-3545-01")) == 11


def tpadrejectsmalformed():
    assert enrich.pad("0378-3545") is None    # 2 segments, not a package code
    assert enrich.pad("003783545011") is None  # no hyphens at all


def tallthreesegmentationsaretried():

    cand = enrich._candidates          # was a _cand() wrapper defined below
    # labeler 0378 is 4 digits -> both the stored 5-4 and the 4-4-2 form
    assert cand("00378351501") == ["00378-3515", "0378-3515"]
    # product 678 is 3 digits -> the 5-3-2 form, the branch that was missing
    assert cand("12345067890") == ["12345-0678", "12345-678"]
    # neither segment padded -> only the stored form is possible
    assert cand("12345678901") == ["12345-6789"]
    # both padded -> all three
    assert len(cand("01234056789")) == 3
