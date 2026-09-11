from difflib import get_close_matches

from pharmacli.err import RuleError, UnknownRuleError
from pharmacli.protocols import BaseRule
from pharmacli.rules.dedup import DedupRule
from pharmacli.rules.sort import SortRules

RULES: dict[str, type[BaseRule]] = {"dedup": DedupRule, "sort": SortRules}


def make(name: str, **options: object) -> BaseRule:
    if name not in RULES:
        near = get_close_matches(name, RULES, n=1)
        raise UnknownRuleError(name, near[0] if near else None)
    try:
        return RULES[name](**options)          # type: ignore[arg-type]
    except TypeError as exc:
        raise RuleError(f"rule {name} does not accept those options: {exc}") from exc


def describe() -> list[tuple[str, str]]:
    return [(n, c.description) for n, c in sorted(RULES.items())]
