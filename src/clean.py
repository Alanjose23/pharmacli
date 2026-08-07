#clean method for looking up the amount of dupes and looking at the amount of deleted rows from medicine rows


from dataclasses import dataclass, astuple
import csv
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
    "Explaination Code",
    "Classification for Rate Setting",
    "Corresponding Generic Drug NADAC Per Unit",
    "Corresponding Generic Drug Effective Date",
    "As of Date"
]

SORTCOLUMN = {
    "description": "description",
    "ndc":"ndc",
    "price":"price",
    "date":"effective date"
}

deduped = {"row", 'ndc description'}



# adding one valid row from CSV to  Medicine dataclass

@dataclass
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



#storing duplicate and throwaway items
@dataclass
class dupes:
    line: int
    errortype: str



#reader/iterator function
def readrows(path: str):
    with open(path, newline="", encoding = "utf-8") as filin:
        reader = csv.reader(filin)
        #pass in csv file into reader object

        header = next(reader)
        #if file has heading skip
        try:
            val = next.reader()
            print(val)
        except StopIteration:
            return


        while True:
            try:
                val = next(reader)
            except StopIteration:
                break

            #store value

            ln = reader.line_num
            #create a function that would catalogue read data and organize into results


            if not any(item.strip() for item in val):
                yield dupes(ln, "line is returned as blank")
                continue


            if len(val) != len(COLUMNS):
                yield dupes(ln, "items in the dataset are not matched up with the class")
                continue

            stval = [item.strip() for item in val]

            if stval[0] == COLUMNS[0]:
                yield dupes(ln, "repeated header")

            if not stval[0] and not stval[1]:
                yield dupes(ln, "missing NDC")
                continue

            if not stval[1]:
                yield dupes(ln, "missing description")
                continue

            yield Medicine(val[0], val[1], val[2],val[3],val[4], val[5], val[6], val[7], val[8], val[9], val[10],val[11])
            #formatted string change needed change val 2 to price as it is a float

           
                

# removed and changed the tuple to organize into skipped and recorded lists
def orgrec(path:str) -> tuple[tuple[Medicine,...],tuple[dupes,...]]:
    medicine: list[Medicine] = []
    skipped: list[dupes] = []

    # return tuples to be put into two categories cab and noncab
    for row in readrows(path):
        if isinstance(row,Medicine):
            medicine.append()
        else:
            skipped.append()
    return tuple(medicine), tuple(skipped)


#count duplicated lines using dedup
def dedup(medicine, key:str = "row") -> tuple[tuple[Medicine,...], int]:

    if key not in deduped:
        raise ValueError("unkown key used, please use ndc or description")

    if key == "row":
        nondupe = tuple(dict.fromkeys(medicine))

    else:
        val1 = attrgetter("ndc") if key == "ndc" else attrgetter("ndc", "effective_date")
        seen: set = set()
        out = []
        for meds in medicine:
            mark = val1(meds)
            if mark not in seen:
                seen.add(mark)
                out.append(medicine)

        kept = tuple(out)

    numdupes = len(medicine) - len(kept)

    return kept,numdupes 
    





#sorter object
def sorter(medicine, by: str = "description") -> tuple[Medicine, ...]
    if by not in SORTCOLUMN:
        raise ValueError("Unnknown sort column, please use another value to sort by")
    # used attrgetter to sort items by selected column value
    return tuple(sorted(medicine, key=attrgetter(SORTCOLUMN[by])))





# writer object
def writer(medicine, stream) -> int:

    writer = csv.writer(stream)
    writer.writerow(COLUMNS)

    for record in medicine:
        print(record)
        writer.writerow(astuple(record))

    return len(medicine)



