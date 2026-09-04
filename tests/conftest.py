"""Shared test helpers.

`row()` lived twice -- once in test_frame.py, once in test_model.py -- building
the same 12-column RawRow from COLUMNS. The model copy was the frame copy with
rate/generic fixed, so one function with defaults covers both. conftest.py is
pytest's own place for this, so it needs no package or import path of its own.
"""

from pharmacli.models.rows import COLUMNS, RawRow


def row(ndc: str, price: str, asof: str, rate: str = "G", generic: str = "") -> RawRow:
    """One NADAC CSV row as the reader would hand it to Medicine.arow."""
    return dict(  # type: ignore[return-value]
        zip(COLUMNS,
            [f"DRUG {ndc}", ndc, price, "01/01/2026", "EA", "C/I", "N", "1",
             rate, generic, "01/01/2026" if generic else "", asof]))
