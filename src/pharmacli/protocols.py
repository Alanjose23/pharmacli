from collections.abc import Sequence
from typing import Protocol

from pharmacli.models.medicine import Medicine

# (surviving records, how many this rule removed). The count is not bookkeeping:
# `pharmacli clean` prints it per rule, and a rule that removed everything looks
# identical to one that did nothing without it. `sort` always reports 0, which
# is the honest answer for a rule that reorders rather than filters.
RuleOut = tuple[tuple[Medicine, ...], int]


class BaseRule(Protocol):
    name: str
    description: str      # what `pharmacli rules` prints

    def apply(self, records: Sequence[Medicine]) -> RuleOut: ...
