# set every input in pharma module 
    # import rules last(so every rule registers first)

from pharmacli.err import (
    DuplicateRuleError,
    HeaderMismatch,
    InputError,
    PharmaError,
    RuleError,
    UnknownRuleError,
)
from models.medicine import Drug, Medicine, Price, parsedate
from models.rows import COLUMNS, RawRow, SkippedRow
from pharmacli.output import open_output
from pharmacli.pipeline import Cleaner, CleanReport
from pharmacli.protocols import BaseRule, Rule, RuleRes
from pharmacli.reader import CSVR, ParseReport
from pharmacli.registry import RULES, Registry
from pharmacli.writer import CSVW
from pharmacli import rules  # noqa: F401  # import last so every rule registers

__version__ = "2.0"

__all__ = [  # noqa: RUF022
    "COLUMNS",
    "BaseRule",
    "CleanReport",
    "Cleaner",
    "CSVR",
    "CSVW",
    "Drug",
    "DuplicateRuleError",
    "HeaderMismatch",
    "InputError",
    "Medicine",
    "ParseReport",
    "PharmaError",
    "Price",
    "RULES",
    "RawRow",
    "Registry",
    "Rule",
    "RuleError",
    "RuleRes",
    "SkippedRow",
    "UnknownRuleError",
    "open_output",
    "parsedate",
]