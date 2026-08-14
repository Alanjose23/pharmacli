##Medicine Data Parser importing data to transform into a class
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from models.rows import RawRow, SkippedRow

# interpreter for values for comparison


# interpret date formats
DATES: Final[tuple[str,...]] = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y")

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


    @classmethod #build medicine from row and line num
    def arow(cls, row: RawRow, lint:int) -> "Medicine | SkippedRow":
    #define rows as validated or skipped
        ndc = row["NDC"].strip()
        desc = row["NDC Description"].strip()
        rprice = row["NADAC Per Unit"]. strip()
        dateeff = parsedate(row["Effective Date"])
        asofd = parsedate(row["As of Date"])
        otc = parseotc(row["OTC"])
        genrow = row["Corresponding Generic Drug NADAC Per Unit"].strip()
        trueprice = parseprice(rprice)


        

# define missing exceptions
        if not ndc:
            return SkippedRow(lint, "Missing NDC")
        if not desc:
            return SkippedRow(lint, "Missing Description")

        
        if trueprice is None:
            return SkippedRow(lint, "Price is not found")


        genprice = parseprice(genrow)
        if genprice is not None:
            gendate = parsedate(row["Corresponding Generic Drug Effective Date"])
            generic = Price (unitprice=genprice, unit= row['Pricing Unit'].strip(), expirydate=gendate)
        else:
            generic = None


        drug = Drug(ndc = ndc, description= desc)
        price = Price(unitprice=trueprice, unit=row['Pricing Unit'].strip(), expirydate=dateeff)

        return cls(
            drug= drug,
            price = price,
            pharmacytype = row['Pharmacy Type Indicator'].strip(),
            otc=otc,
            explanation = row['Explanation Code'].strip(),
            rateclass = row['Classification for Rate Setting'].strip(),
            generic = generic,
            asofdate=asofd
        )

    def torow(self) -> list[str]:
        return[
            self.drug.description,
            self.drug.ndc,
            f"{self.price.unitprice}",
            self.formatdate(self.price.expirydate),
            self.price.unit,
            self.pharmacytype,
            "Y" if self.otc else "N",
            self.explanation,
            self.rateclass,
            f"{self.generic.unitprice}" if self.generic else "",
            self.formatdate(self.generic.expirydate) if self.generic else "",
            self.formatdate(self.asofdate)



        ]

    @staticmethod
    def formatdate(value: date | None) -> str:
        return value.isoformat() if value is not None else ""

    @property
    def sortprice(self) -> float:
        return self.price.unitprice

    @property
    def sort_date(self) -> tuple[bool,date]:

        if self.price.expirydate is None:
            return (True, date.min)
        return (False, self.price.expirydate)

        
