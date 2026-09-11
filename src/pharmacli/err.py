# define a base pharma cli error

from pathlib import Path


class PharmaError(Exception):
    pass
    
# input error : path and reason
class InputError(PharmaError):
    def __init__(self, path: Path, reason:str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(reason)

# define column mismatch
class HeaderMismatch(InputError):
    # header mismatched reworked for inventory so that the inventory csv can be consumed
    # takes counts, not the column tuples: the old signature accepted both
    # sequences and used nothing but len() on them
    def __init__(self, path: Path, expected: int, found: int) -> None:
        super().__init__(path, f"Header Mismatch, {expected} columns but {found} columns found")

# custom errors

# rule errors
    # define a rule error
class RuleError(PharmaError):
    pass


# unknown rule error
class UnknownRuleError(RuleError):
    def __init__(self, name: str | None = None, suggestion: str | None = None) -> None:
        self.name = name
        self.suggestion = suggestion
        hint = f", did you mean {suggestion!r}?" if suggestion else ""
        super().__init__(f"unknown rule {name}{hint}")


# REMOVED: DuplicateRuleError. It could only fire if two rules registered the
# same name, which needed a decorator registry to be possible at all. The rule
# table is now a plain dict literal -- a duplicate key is a syntax-level
# mistake, not a runtime error class.


# page error for api
class FetchError(PharmaError):
    pass
