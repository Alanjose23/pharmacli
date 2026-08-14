# holds a list of rules, that the CLI ddisplays

from collections.abc import Sequence
from dataclasses import dataclass, field

from .models.medicine import Medicine
from .protocols import Rule


@dataclass
class CleanReport:
    # results of a run
    records: tuple[Medicine, ...]
    per_rule: tuple[tuple[str,str],...]
    notes: tuple[str, ...] = field(default_factory= tuple)

    @property
    def totalremoved(self) -> int:
        return sum(removed for _, removed in self.per_rule)



class Cleaner:
    #holding an ordered set of rules that feeds records

    #tuple of rules
    rules: tuple[Rule, ...] = field(default_factory=tuple)

    def run(self, records: Sequence[Medicine]) -> CleanReport:
        working: tuple[Medicine, ...] = tuple(records)
        per_rule : list[tuple[str, int]] = []
        notes: list[str] = []
        # append items and create an actual report that can be usable
        for rule in self.rules:
            result = rule.apply(working)
            working = result.records
            per_rule.append((rule.name, result.removed))
            notes.extend(result.notes)


        return CleanReport(records=working, per_rule=tuple(per_rule), notes= tuple(notes))
