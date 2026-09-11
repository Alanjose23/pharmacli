from pharmacli.models.rows import COLUMNS, RawRow


def row(ndc: str, price: str, asof: str, rate: str = "G", generic: str = "") -> RawRow:
    return dict(  # type: ignore[return-value]
        zip(COLUMNS,
            [f"DRUG {ndc}", ndc, price, "01/01/2026", "EA", "C/I", "N", "1",
             rate, generic, "01/01/2026" if generic else "", asof]))
