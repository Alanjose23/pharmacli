# turns row of text into dataclass while using the code

from dataclasses import dataclass
from datetime import date, datetime
# for price
from decimal import Decimal, InvalidOperation
from typing import Final

from pharmacli.models.rows import RawRow, SkippedRow

# the date layouts from csv and api 
DATES: Final[tuple[str,...]] = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y")

OTCVALS: Final[frozenset[str]] = frozenset({"y","yes","true", "1", "otc"})
OTCNO: Final[frozenset[str]] = frozenset({"n","no","false","0","rx"})


# formatting dates from sources and making sure they are valid
def parsedate(raw: str) -> date | None:
    text = raw.strip()
    if not text:
        return None
    for fmt in DATES:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# (price, None) or (None, why): missing, malformed or impossible price
def parseprice(raw: str) -> tuple[Decimal | None, str | None]:
    text = raw.strip()
    if not text:
        return None, "missing price"

    try:
        value = Decimal(text)
    except InvalidOperation:
        return None, "malformed price"

    if not value.is_finite():
        return None, "malformed price"

    if value <= 0:
        return None, "non-positive price"

    return value, None


# returns if it is an otc drug, makes sure a value is assigned
def parseotc(raw: str) -> bool | None:
    text = raw.strip().lower()
    if text in OTCVALS:
        return True
    if text in OTCNO:
        return False
    return None

# drug dataclass
@dataclass(frozen=True, slots=True)
class Drug:
    ndc: str
    description: str


@dataclass(frozen=True, slots=True)
class Price:
    unitprice: Decimal
    unit: str
    #changed from expiry date as this is a price
    effective_date: date | None


@dataclass(frozen=True, slots=True)
class Medicine:
    drug: Drug
    price: Price
    pharmacytype: str
    otc: bool | None
    explanation: str
    rateclass: str
    generic: Price | None
    asofdate: date | None

    # if Medicine and if not
    @classmethod
    def arow(cls, row: RawRow, line_no: int) -> "Medicine | SkippedRow":
        ndc = row["NDC"].strip()
        desc = row["NDC Description"].strip()
        rprice = row["NADAC Per Unit"].strip()
        dateeff = parsedate(row["Effective Date"])
        asofd = parsedate(row["As of Date"])
        otc = parseotc(row["OTC"])
        genrow = row["Corresponding Generic Drug NADAC Per Unit"].strip()
        unitprice, why = parseprice(rprice)

        if not ndc:
            return SkippedRow(line_no, "Missing NDC")
        if not desc:
            return SkippedRow(line_no, "Missing Description")

        # passing on the specific reason
        if unitprice is None:
            return SkippedRow(line_no, why or "malformed price")

        # a generic price should not kill the row
        genprice, _ = parseprice(genrow)
        if genprice is not None:
            gendate = parsedate(row["Corresponding Generic Drug Effective Date"])
            generic = Price(unitprice=genprice, unit=row["Pricing Unit"].strip(),effective_date=gendate)
        else:
            generic = None

        drug = Drug(ndc=ndc, description=desc)
        price = Price(unitprice=unitprice, unit=row["Pricing Unit"].strip(),effective_date=dateeff)

        return cls(
            drug=drug,
            price=price,
            pharmacytype=row["Pharmacy Type Indicator"].strip(),
            otc=otc,
            explanation=row["Explanation Code"].strip(),
            rateclass=row["Classification for Rate Setting"].strip(),
            generic=generic,
            asofdate=asofd,
        )

    # writing row back out as a list
    def torow(self) -> list[str]:
        return [
            self.drug.description,
            self.drug.ndc,
            f"{self.price.unitprice}",
            self.formatdate(self.price.effective_date),
            self.price.unit,
            self.pharmacytype,
            self.formatotc(self.otc),
            self.explanation,
            self.rateclass,
            f"{self.generic.unitprice}" if self.generic else "",
            self.formatdate(self.generic.effective_date) if self.generic else "",
            self.formatdate(self.asofdate),
        ]


    #static method to make sure that each row is standardized and given a value
    @staticmethod
    def formatdate(value: date | None) -> str:
        return value.isoformat() if value is not None else ""

    # formatting otc with options
    @staticmethod
    def formatotc(value: bool | None) -> str:
        if value is None:
            return ""
        return "Y" if value else "N"

    # REACHED BY NAME, not by call: rules/sort.py maps --by date onto this
    # through attrgetter(), so a grep reports it dead. It is not.
    @property
    def sort_date(self) -> tuple[bool, date]:
        if self.price.effective_date is None:
            return (True, date.min)
        return (False, self.price.effective_date)
