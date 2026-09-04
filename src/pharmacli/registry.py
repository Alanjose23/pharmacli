from collections.abc import Callable
from difflib import get_close_matches
from typing import Any

from pharmacli.err import DuplicateRuleError, RuleError, UnknownRuleError
from pharmacli.protocols import BaseRule


class Registry:
#mapped to class
    def __init__(self) -> None:
        self._items: dict[str, type[BaseRule]] = {}

    def register(self, name: str) -> Callable[[type[BaseRule]], type[BaseRule]]:
        #files class under a public name 
        def decorate(cls: type[BaseRule]) -> type[BaseRule]:
            if name in self._items:
                raise DuplicateRuleError(name)
            self._items[name] = cls
            return cls

        return decorate

    # new rule creation
    def get(self, name: str) -> type[BaseRule]:
        if name not in self._items:
            clse = get_close_matches(name, self._items.keys(), n=1)
            raise UnknownRuleError(name, clse[0] if clse else None)
        return self._items[name]

    # builds a rule, callers only have to handle rule errors
    def create(self, name: str, **options:Any) -> BaseRule:
        # instance of named class
        cls = self.get(name)
        try:
            return cls(**options)
        except TypeError as exc:
            raise RuleError(f"rule {name} does not accept those options: {exc}") from exc

    def names(self) -> tuple[str,...]:
        # --choices here 
        return tuple(sorted(self._items))

    def describeall(self) -> tuple[tuple[str,str],...]:
        # name with summary for rules
        pairs = []
        for name in sorted(self._items):
            cls = self._items[name]
            docs = (cls.__doc__ or "").strip()
            fl = docs.splitlines()[0] if docs else ""
            pairs.append((name, fl))
        return tuple(pairs)

RULES: Registry = Registry()
# shared rule registry for CLI py access

