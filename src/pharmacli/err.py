# define a base pharma cli error


class PharmaError(Exception):
    pass


# an unreadable input file or a header that does not match; the message names both
class InputError(PharmaError):
    pass

# custom errors

# rule errors
    # define a rule error
class RuleError(PharmaError):
    pass


# unknown rule error
class UnknownRuleError(RuleError):
    def __init__(self, name: str | None = None, suggestion: str | None = None) -> None:
        hint = f", did you mean {suggestion!r}?" if suggestion else ""
        super().__init__(f"unknown rule {name}{hint}")


# page error for api
class FetchError(PharmaError):
    pass
