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
    def __init__ (self,path:Path, expected: tuple[str,...], found: tuple[str,...]) -> None:
        super().__init__(path, f"Header Mismatch, {len(expected)} columns but {len(found)} columns found")

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


# duplicate rule error
class DuplicateRuleError(RuleError):
    def __init__(self, name:str) -> None:
        self.name = name
        super().__init__(f"{name} is already a Rule")


# page error for api
class FetchError(PharmaError):
    pass
