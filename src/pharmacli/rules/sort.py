from collections.abc import Sequence
from operator import attrgetter
from typing import ClassVar, Final

from pharmacli.err import RuleError
from pharmacli.models.medicine import Medicine
from pharmacli.protocols import RuleOut

# possible sorted columns
COLUMNS: Final[dict[str,str]] = {
    "description": "drug.description",
    "ndc": "drug.ndc",
    "price": "price.unitprice",
    "date": "sort_date"
}


class SortRules:
    # not decoration: rules.describe() prints this for `pharmacli rules`
    description = "Order records by description, NDC, price, or effective date."

    name: ClassVar[str] = "sort"

    def __init__(self, by:str="description" ) -> None:
        if by not in COLUMNS:
            raise RuleError(f"unknown column, select from {COLUMNS}")
        self.by = by




# built sort from identified column
    # apply rule to sequence of medicines
    def apply(self, records: Sequence[Medicine]) -> RuleOut:
        # determine ordering

        # arrange by order
        key = attrgetter(COLUMNS[self.by])
        ordered = tuple(sorted(records, key = key))

        return ordered, 0                # sorting drops nothing, ever
# return rule result