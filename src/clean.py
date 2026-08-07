#clean method for looking up the amount of dupes and looking at the amount of deleted rows from medicine rows


import csv
from dataclasses import astuple, dataclass
from datetime import datetime
from operator import attrgetter

#references for measurement and copying to another file
# tried to use readerdict, but some values were consumed

COLUMNS = [
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
    "As of Date"
]

SORTCOLUMN = {
    "description": "description",
    "ndc":"ndc",
    "price":"price_value",
    "date":"date_value"
}

deduped = {"row", 'ndc', "ndc+date"}

DATEFORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y")

UNPARSEABLE_DATE = datetime.max




# adding one valid row from CSV to  Medicine dataclass

@dataclass(frozen=True)
class Medicine:
    description: str
    ndc: str
    price: str
    edate: str
    punit: str
    phtype: str
    otc: str
    excode: str
    rclass:str
    gprice: str
    gdate: str
    asdate: str

    @property
    def price_value(self) -> float:
        try:
            return float(self.price)
        except ValueError:
            return float("inf")

    @property
    def date_value(self) -> datetime:
        for fmt in DATEFORMATS:
            try:
                return datetime.strptime(self.edate, fmt)
            except ValueError:
                continue
            return UNPARSEABLE_DATE



#storing duplicate and throwaway items
@dataclass
class dupes:
    line: int
    reason: str



#reader/iterator function
def readrows(path: str):
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as filin:
        reader = csv.reader(filin)

        
        try:
            next(reader)
        except StopIteration:
            return

        firstcol = COLUMNS[0].casefold()
 
        
        for val in reader:
            
            ln = reader.line_num
 
           
            if not any(item.strip() for item in val):
                yield dupes(ln, "blank line")
                continue
 
            
            if len(val) != len(COLUMNS):
                yield dupes(ln, f"expected {len(COLUMNS)} fields, found {len(val)}")
                continue
 
            
            stval = [item.strip() for item in val]
 
           
            if stval[0].casefold() == firstcol:
                yield dupes(ln, "repeated header row")
                continue
 
         
            if not stval[1]:
                yield dupes(ln, "missing NDC")
                continue
 
            if not stval[0]:
                yield dupes(ln, "missing description")
                continue
 
           
            try:
                float(stval[2])
            except ValueError:
                yield dupes(ln, f"price is not a number: {stval[2]!r}")
                continue
 
            
            yield Medicine(*stval)
            #formatted string change needed change val 2 to price as it is a float

           
                

# removed and changed the tuple to organize into skipped and recorded lists
def orgrec(path:str) -> tuple[tuple[Medicine,...],tuple[dupes,...]]:
    medicine: list[Medicine] = []
    skipped: list[dupes] = []

    # return tuples to be put into two categories cab and noncab
    for row in readrows(path):
        if isinstance(row,Medicine):
            medicine.append(row)
        else:
            skipped.append(row)
    return tuple(medicine), tuple(skipped)


#count duplicated lines using dedup
def dedup(medicine, key:str = "row") -> tuple[tuple[Medicine,...], int]:

    if key not in deduped:
        raise ValueError("unkown key used, please use ndc or description")

    if key == "row":
        kept = tuple(dict.fromkeys(medicine))

    else:
        val1 = attrgetter("ndc") if key == "ndc" else attrgetter("ndc", "edate")
        seen: set = set()
        out = []
        for meds in medicine:
            mark = val1(meds)
            if mark not in seen:
                seen.add(mark)
                out.append(meds)

        kept = tuple(out)

    numdupes = len(medicine) - len(kept)

    return kept,numdupes 
    





#sorter object
def sorter(medicine, by: str = "description") -> tuple[Medicine, ...]:
    if by not in SORTCOLUMN:
        raise ValueError("Unnknown sort column, please use another value to sort by")
    # used attrgetter to sort items by selected column value
    return tuple(sorted(medicine, key=attrgetter(SORTCOLUMN[by])))





# writer object
def writer(medicine, stream) -> int:

    writer = csv.writer(stream, lineterminator= "\n")
    #lineterminator needed for consuming other newer lines
    writer.writerow(COLUMNS)

    for record in medicine:
        
        writer.writerow(astuple(record))

    return len(medicine)



