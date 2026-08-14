# rule creation using an abstract method 
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Protocol, runtime_checkable

from models.medicine import Medicine


@dataclass(frozen=True, slots=True)
class RuleRes:
    #overview of what a rule's production
    records: tuple[Medicine,...]
    removed: int
    notes: tuple[str, ...] = field(default_factory = tuple)

@runtime_checkable
class Rule(Protocol):
    #typing that does satisfy rule with matching name and application
    name:ClassVar[str]

    def apply(self, records: Sequence[Medicine]) -> RuleRes: ...


class BaseRule(ABC):
    # conventions for the same built in rules
    name: ClassVar[str] = ""

    @abstractmethod
    def apply(self, records: Sequence[Medicine]) -> RuleRes: ...

    def describe(self) -> str:
        # summary for pharma cli rules
        doc = (self.__doc__ or "").strip()
        fl = doc.splitlines()[0] if doc else ""
        return f"{self.name}: {fl}"
#