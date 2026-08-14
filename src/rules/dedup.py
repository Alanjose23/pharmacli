

from collections.abc import Sequence
from operator import attrgetter
from typing import ClassVar, Literal

from ..models.medicine import Medicine
from ..protocols import BaseRule, RuleRes
from ..registry import RULES

DedupK = Literal["row", "ndc", "ndc + date"]

# register dedup rule and add the previous iterations to the code
@RULES.register("dedup")
class DedupRule(BaseRule):
    name: ClassVar[str] = "dedup"

        

# choose key (row, ndc, ndc and day)
    def __init__(self, key: DedupK = "row") -> None:
        self.key: DedupK = key

    def apply(self, records: Sequence[Medicine]) -> RuleRes:
        if self.key == "row":

            kept: tuple[Medicine, ...] = tuple(dict.fromkeys(records))

        else:

            identity = (

                attrgetter("drug.ndc")
                if self.key == "ndc"
                else attrgetter("drug.ndc", "price.expirydate")

            )
            seen: set[object] = set()
            out: list[Medicine] = []# if identity has been seen, remember the identity and compare and see if any others follow it
            for record in records:
                mark = identity(record)
                if mark not in seen:
                    seen.add(mark)
                    out.append(record)
            kept = tuple(out)


        removed = len(records) - len(kept)


        # return a result holding kept medicines and removed(should be a count variable)
        return RuleRes(records=kept, removed = removed)







