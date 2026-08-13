##Medicine Data Parser importing data to transform into a class
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from models.rows import RawRow, SkippedRow

# interpreter for values for comparison


# interpret date formats
DATES = Final[tuple[str,...]] = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y")

OTCVALS: Final[frozenset[str]] = frozenset({"y","yes","true", "1", "otc"})
# values for conversion to boolean
def parsedate(raw: str) -> date | None:
    text = raw.strip()
    if not text:
        return None
    for format in DATES: 
        try:
            return datetime.strptime(text, format).date()
        except ValueError: 
            continue


    return None



# interpret the price

def parseprice(raw: str) -> float | None:
    text = raw.strip()
    if not text:
        return None
    try: 
        if float(text) == 0.0:
            return None
        if float(text) < 0.0:
            return None
        
        return float(text)
    except ValueError:
        return None


# otc conversion format
def parseotc(raw: str) -> bool:
    return raw.strip().lower() in OTCVALS

# define a drug format as NDC and description
@dataclass(frozen=True, slots=True)
class Drug: 
    ndc: str
    description: str


@dataclass(frozen=True, slots=True)
class Price: 
    unitprice: float
    unit: str
    expirydate: str | None



# redefine the medicine
@dataclass(frozen=True, slots=True)
class Medicine: 
    drug: Drug
    price: Price
    pharmacytype: str
    otc: bool
    explanation: str
    rateclass: str
    generic: Price | None
    asofdate:date | None

#build medicine from row and line num

# define missing exceptions

