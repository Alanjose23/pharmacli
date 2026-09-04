# holds a list of rules, that the CLI ddisplays

from collections.abc import Sequence
from dataclasses import dataclass, field

from pharmacli.models.medicine import Medicine
from pharmacli.protocols import BaseRule


@dataclass
class CleanReport:
    # results of a run
    records: tuple[Medicine, ...]
    # offical record and how many rules each one removed
    per_rule: tuple[tuple[str,int],...]




@dataclass
class Cleaner:
    #holding an ordered set of rules that feeds records

    #tuple of rules
    rules: tuple[BaseRule, ...] = field(default_factory=tuple)

    def run(self, records: Sequence[Medicine]) -> CleanReport:
        working: tuple[Medicine, ...] = tuple(records)
        per_rule : list[tuple[str, int]] = []
        # append items and create an actual report that can be usable
        for rule in self.rules:
            result = rule.apply(working)
            working = result.records
            per_rule.append((rule.name, result.removed))


        return CleanReport(records=working, per_rule=tuple(per_rule))
