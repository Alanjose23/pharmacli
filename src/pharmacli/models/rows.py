# columns and reasons for code
from dataclasses import dataclass
from typing import Final, TypedDict

COLUMNS: Final[tuple[str,...]] = (
    "NDC Description",
    "NDC",
    "NADAC Per Unit",
    "Effective Date",
    "Pricing Unit",
    "Pharmacy Type Indicator",
    "OTC",
    "Explanation Code",
    "Classification for Rate Setting",
    "Corresponding Generic Drug NADAC Per Unit",
    "Corresponding Generic Drug Effective Date",
    "As of Date",
)

# Raw Row from CSV or API
RawRow = TypedDict(
    "RawRow",
    {
        "NDC Description":str,
        "NDC":str,
        "NADAC Per Unit":str,
        "Effective Date":str,
        "Pricing Unit":str,
        "Pharmacy Type Indicator":str,
        "OTC":str,
        "Explanation Code":str,
        "Classification for Rate Setting":str,
        "Corresponding Generic Drug NADAC Per Unit":str,
        "Corresponding Generic Drug Effective Date":str,
        "As of Date":str
    }
)


@dataclass(frozen=True, slots=True)
class SkippedRow:
    line:int
    reason:str

    def __str__(self) -> str:
        return f"line {self.line}: reasoning: {self.reason}"
