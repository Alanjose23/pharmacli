from collections.abc import Sequence
from operator import attrgetter
from typing import ClassVar, Final

from ..err import RuleError
from ..models.medicine import Medicine
from ..protocols import BaseRule, RuleRes
from ..registry import RULES

# define sortable columns(description, ndc, price, and date)
COLUMNS: Final[dict[str,str]] = {
    "description": "drug.description",
    "ndc": "drug.ndc",
    "price": "sort_price",
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