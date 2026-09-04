# rule creation using an abstract method 
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from pharmacli.models.medicine import Medicine


@dataclass(frozen=True, slots=True)
class RuleRes:
    #overview of what a rule's production
    records: tuple[Medicine,...]
    removed: int


class BaseRule(ABC):
    # conventions for the same built in rules
    name: ClassVar[str] = ""

    @abstractmethod
    def apply(self, records: Sequence[Medicine]) -> RuleRes: ...