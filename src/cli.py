import argparse
import os
import sys
from pathlib import Path
from typing import Final

from ..models.rows import COLUMNS

# import reader,writer, registry, pipeline, registry, models, errors
from .err import PharmaError, RuleError
from .output import open_output
from .pipeline import Cleaner
from .protocols import Rule
from .reader import CSVR
from .registry import RULES
from .writer import CSVW


VERSION: Final[str] = "2.0.0"
MAX_SKIPPED: Final[int] = 50
# entry point of code

#exit code definition
    # 0 = success
EXIT_OK: Final[int] = 0
    # 1 = no usable rows
EXIT_NOROWS: Final[int] = 1
    # 2 for bad input
EXIT_BADIN: Final[int] = 2
    # 3 for bad rule
EXIT_BADRULE: Final[int] = 3


# display errors
def warn(message: object) -> None:
    print(message, file= sys.stderr)

# rule recongition
def parserule(raw: str) -> tuple[str, dict[str,str]]:

    if ":" not in raw:
        return raw, {}

    name, _, optiontext = raw.partition(":")
    options: dict[str, str] = {}
    for setting in optiontext.split(","):
        if "=" not in setting:
            raise RuleError(f"expected 'key=value' in rule options,got {setting!r}")
        key, _, value = setting.partition("=")
        options[key.strip()] = value.strip()
    return name.strip(), options
    # define a rule and its identifying key
    # handle rule misdiagnosis and representation
    # add errors to definition

# build command line parser
def buildparse() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog= "PharmaCLI", description= "CLI tool for parsing inventory CSVs", epilog= "use -n/ --dryrun to see what would change without writing",)
    parser.add_argument("-v","--version", action = "version", version = f"PharmaCLI {VERSION}")

    # define output input and dryrun tags
        
    
    fileopts = argparse.ArgumentParser(add_help=False)
    fileopts.add_argument("input", type = Path, help = "path to input CSV")
    fileopts.add_argument("-o", "--outfile", type=Path, default=None, help="write to here instead of stdout")
    fileopts.add_argument("-n", "--dryrun", action='store_true', help = "report only, write nothing")



    commands = parser.add_subparsers(dest="command", required= True, metavar="COMMAND")
    # add new rules command
    clean = commands.add_parser("clean", parents=[fileopts], help="run one or more rules")
    clean.add_argument("--rule", action="append", default=[], metavar="NAME[:KEY=VALUE, ...]", help="repeatable; avaliable {', '.join(RULES.names())}")


    dedup = commands.add_parser("dedup", parents=[fileopts], help= "remove dupes")
    dedup.add_argument("--key", default= "row", choices= ('row', 'ndc', 'ndc+date'), help = "what makes the rows duplicates")
    sortd = commands.add_parser("sort", parents=[fileopts], help= "sorts all data by column name")
    sortd.add_argument("--by", default="description",choices = {'description', 'ndc', 'price', 'date'},help = "select the column in which you want to sort the column by, default is by description")

    # list rules and 

    # list columns 
    commands.add_parser("rules", help="list every registry approved tool")
    commands.add_parser("columns", help="list the expected columns")

    return parser


# obtain arguments from command line grammar
    # return rules, columns, or parse report with input line
    # display common error faults( no file found and lines detected
    
def cmdrules() -> int:
    for name, summ in RULES.describeall():
        print(f"{name: <16} {summ}")
    return EXIT_OK

def cmdcol() -> int:
    for pos, col in enumerate(COLUMNS, start=1):
        print(f"{pos:>2}. {col}")
    return EXIT_OK


# add and init rules with request
    # create a configured instance
# display rule set and if there is a bad rule:reject
def buildrule(args: argparse.Namespace) -> list[Rule] | None:


    if args.command == "dedup":
        return RULES.create("dedup", key=args.key)

    if args.command == "sort":
        return RULES.create("sort", key=args.by)

    built: list[Rule] = []
    for raw in args.rule:
        try:
            name, options = parserule(raw)
            built.append(RULES.create(name, **options))
        except (RuleError, TypeError) as exc:
            warn(f"error: {exc}")
            return None
    return built

def dispatch(args: argparse.Namespace) -> int:
    if args.command == "rules":
        return cmdrules
    
    if args.command == "columns":
        return cmdcol

    try:
        report = CSVR(args.input).read()
    except PharmaError as exc:
        warn(f"error: {exc}")
        return EXIT_BADIN

    warn(f"understood: {len(report.records)} items and skipped {len(report.skipped)}")
    for row in report.skipped[:MAX_SKIPPED]:
        warn(row)


    if not report.records:
        warn("No more usable rows found")
        return EXIT_NOROWS

    rules = buildrule(args)
    if rules is None:
        return EXIT_BADRULE

    clean = Cleaner(tuple(rules)).run(report.records)

    for name, removed in clean.per_rule:
        warn(f"{name} : rows removed : {removed}")
    for note in clean.notes:
        warn(note)

    outpath = args.outfile or "stdout"
    if args.dryrun:
        warn(f"dry run results: could write {len(clean.records)} rows to {outpath}")
        return EXIT_OK

    try:
        with open_output(args.outfile) as stream:
            written = CSVW(stream).write(clean.records)
    except OSError as exc:
        warn("Cannot be written to outpath")
        return EXIT_BADIN

    warn(f"{written} rows to {outpath}")
    return EXIT_OK



def main(argv: list[str] | None = None) -> int:
    parser = buildparse()
    args = parser.parse_args(argv)


    try:
        return dispatch(args)
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_OK



if __name__ == "__main__":
    raise SystemExit(main())



    # dryrun, rule handling, output file recongnition, and possible errors
    # return success code


# def main(argv: list [str] | None = None) -> int:

#     parser = argparse.ArgumentParser(prog= "pharmacli",description= "CLI Tool for Retail Pharmacy Data",epilog = "-n, --dryrun to return what you would see if it ran")
#     parser.add_argument("-v","--version", action="version",version=f"pharmacli product version: {VERSION}")
#     #prints version as CLI tools do
#     fileparse = argparse.ArgumentParser(add_help=False)
#     #error would occur if defaulted


#     #file io and written structure
#     fileparse.add_argument("input", help="write path to intro csv")
#     fileparse.add_argument("-o","--outfile", help = "written to output file") #std outs to nothing 
#     fileparse.add_argument("-n","--dryrun", help = "report what exactly would change",action = "store_true")
#     #defaults to sorted by name

#     commparse = parser.add_subparsers(dest = "command", required = True, metavar="COMMAND")
#     #subparser to add commands

#     dedup_k = sorted(clean.deduped)
#     sort_k = sorted(clean.SORTCOLUMN)

#     dedup = commparse.add_parser("dedup",parents=[fileparse], help= "removes all duplicates")
#     dedup.add_argument("--key", default= "row", choices= dedup_k, help = "what makes the rows duplicates")
#     dedup.set_defaults(func=fdedup)
#     sortd = commparse.add_parser("sort", parents=[fileparse], help= "sorts all data by column name")
#     sortd.add_argument("--by", default="description",choices = sort_k,help = "select the column in which you want to sort the column by, default is by description")
#     sortd.set_defaults(func=fsorted)
#     #add default as name

#     retcol = commparse.add_parser("col", help= "return columns") 
#     #no parents needed returning just the columns 
#     retcol.set_defaults(func=fretcol)

#     pclean = commparse.add_parser("clean", parents=[fileparse],help= "cleans data by dedup then sort, mocked up by dryrun value")
#     pclean.add_argument("--key", default="row",choices=dedup_k)
#     pclean.add_argument("--by", default="description",choices=sort_k, help= "defaulted to by description")
#     pclean.set_defaults(func=fclean)


    
#     args = parser.parse_args(argv)

#     try:
#         return args.func(args)
#         #each function has its own exit code and that allows it to run safely
#     except Exception as e:
#         errr(f"error: {e}")
#         return 1