from collections.abc import Callable
from difflib import get_close_matches
from inspect import signature
from typing import Any, Generic, TypeVar

from pharmacli.err import DuplicateRuleError, RuleError, UnknownRuleError
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

    # builds a rule, turning a bad option into a RuleError
    #
    # cls(**options) raises a plain TypeError when someone passes an option the
    # rule does not take, and that used to escape as a traceback. converting it
    # here means callers only ever have to handle RuleError
    def create(self, name: str, **options:Any) -> T:
        # instance of named class
        cls = self.get(name)
        try:
            return cls(**options)
        except TypeError as exc:
            accepted = self._accepted(cls)
            raise RuleError(
                f"rule {name} does not accept those options ({exc}); "
                f"accepted options: {accepted}"
            ) from exc

    @staticmethod
    def _accepted(target: type[Any]) -> str:
        # read the rule's own __init__ signature so the message stays correct
        # even when a rule adds or renames a parameter later
        try:
            params = signature(target.__init__).parameters
        except (TypeError, ValueError):
            return "unknown"
        names = [p for p in params if p != "self"]
        return ", ".join(names) if names else "none"

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

