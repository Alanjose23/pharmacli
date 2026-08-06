from dataclasses import dataclass


#references for measurement and copying to another file

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




#tuple addtion and conversion





#sorter object





# writer object




