# define a base pharma cli error

from graphlib import CycleError
from pathlib import Path


class PharmaError(Exception):

    
# input error : path and reason
class InputError(PharmaError):
    def __init__(self, path: Path, reason:str) -> None:
        self.path = path
        self.reason = reason
        super().__init__("Cannot be read")

    # define column mismatch
class headermismatch(InputError):
    def __init__ (self,path:Path, expected: tuple[str,...], found: tuple[str,...]) -> None:
        self.expected = expected
        self.found = found
        super().__init__(path, f"Header Mismatch, {len(expected)} columns but {len(found)} columns found")

# custom errors

# rule errors
    # define a rule error
class RuleError(PharmaError):



# unknown rule error
class UnknownRuleError(RuleError):
    def __init__(self, name:str| None = None) -> None:
        self.name = name
        super().__init__ (f"unknown rule {name}")


# duplicate rule error
class DuplicateRuleError(RuleError):
    def __init__(self, name:str) -> None:
        self.name = name
        super().__init__(f"{name} is already a Rule")

