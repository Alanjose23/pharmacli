from collections.abc import Callable
from difflib import get_close_matches
from typing import Any, Generic, TypeVar

from pharmacli.err import DuplicateRuleError, UnknownRuleError

from pharmacli.protocols import Rule

T = TypeVar('T')

class Registry(Generic[T]):
#mapped to class
    def __init__(self) -> None:
        self._items: dict[str, type[T]] = {}

    def register(self, name: str) -> Callable[[type [T]],type[T]]:
        #files class under a public name 
        def decorate(cls: type[T]) -> type[T]:
            if name in self._items:
                raise DuplicateRuleError(name)
            self._items[name] = cls
            return cls

        return decorate

    #class registered under the name 
    # typed commands into working objects 
    # new rule creation
    def get(self, name: str) -> type[T]:
        if name not in self._items:
            clse = get_close_matches(name, self._items.keys(), n=1)
            raise UnknownRuleError(name, clse[0] if clse else None)
        return self._items[name]

    def create(self, name: str, **options:Any) -> T:
        # instance of named class
        cls = self.get(name)
        return cls(**options)

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

RULES: Registry[Rule] = Registry()
# shared rule registry for CLI py access

