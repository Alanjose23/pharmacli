from dataclasses import dataclass
import csv


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
    price: float
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
            #create a function that would catalogue read data and organize into results

            yield Medicine(val[0], val[1], val[2],val[3],val[4], val[5], val[6], val[7], val[8], val[9], val[10],val[11])
            #formatted string change needed change val 2 to price as it is a float
                






#tuple addtion and conversion





#sorter object





# writer object
def writer(records, stream) -> int:

    writer = csv.writer(stream)
    writer.writerow(COLUMNS)

    for record in records:
        print(record)
        # writer.writerow(astuple(record))

    return len(records)



