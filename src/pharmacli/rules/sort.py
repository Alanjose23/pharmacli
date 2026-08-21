from collections.abc import Sequence
from operator import attrgetter
from typing import ClassVar, Final

from pharmacli.err import RuleError
from pharmacli.models.medicine import Medicine
from pharmacli.protocols import BaseRule, RuleRes
from pharmacli.registry import RULES

# possible sorted columns
COLUMNS: Final[dict[str,str]] = {
    "description": "drug.description",
    "ndc": "drug.ndc",
    "price": "sortprice",
    "date": "sort_date"
}


@RULES.register("sort")
class SortRules(BaseRule):
    

    name: ClassVar[str] = "sort"

    def __init__(self, by:str="description" ) -> None:
        if by not in COLUMNS:
            raise RuleError(f"unknown column, select from {COLUMNS}")
        self.by = by




# built sort from identified column
    # apply rule to sequence of medicines
    def apply(self, records: Sequence[Medicine]) -> RuleRes:
        # determine ordering

        # arrange by order
        key = attrgetter(COLUMNS[self.by])
        ordered = tuple(sorted(records, key = key))

        return RuleRes(records=ordered, removed = 0)
# return rule result