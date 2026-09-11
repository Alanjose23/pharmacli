from collections.abc import Sequence
from operator import attrgetter
from typing import ClassVar, Literal

from pharmacli.models.medicine import Medicine
from pharmacli.protocols import RuleOut

# cli py rules to order by dedup
DedupK = Literal["row", "ndc", "ndc+date"]

class DedupRule:
    description = "Remove duplicate records by row, NDC, or NDC+effective date."

    name: ClassVar[str] = "dedup"

        

# choose key (row, ndc, ndc and day)
    def __init__(self, key: DedupK = "row") -> None:
        self.key: DedupK = key

    def apply(self, records: Sequence[Medicine]) -> RuleOut:
        if self.key == "row":

            kept: tuple[Medicine, ...] = tuple(dict.fromkeys(records))

        else:

            # recongnizing each value using the key values
            identity = (

                attrgetter("drug.ndc")
                if self.key == "ndc"
                else attrgetter("drug.ndc", "price.effective_date")

            )
            seen: set[object] = set()
            out: list[Medicine] = []
            # remember identity
            for record in records:
                mark = identity(record)
                if mark not in seen:
                    seen.add(mark)
                    out.append(record)
            kept = tuple(out)


        removed = len(records) - len(kept)
        # returns what should be kept and what should be removed
        return kept, removed







