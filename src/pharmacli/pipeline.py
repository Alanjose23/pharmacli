from collections.abc import Sequence

from pharmacli.models.medicine import Medicine
from pharmacli.protocols import BaseRule


def clean(rules: Sequence[BaseRule],
          records: Sequence[Medicine]) -> tuple[tuple[Medicine, ...], tuple[tuple[str, int], ...]]:
    working = tuple(records)
    per_rule = []
    for rule in rules:
        working, removed = rule.apply(working)
        per_rule.append((rule.name, removed))
    return working, tuple(per_rule)
