import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Final

from pharmacli.models.rows import COLUMNS

# import reader,writer, registry, pipeline, registry, models, errors
from pharmacli.config import load
from pharmacli.err import FetchError, PharmaError, RuleError
from pharmacli.output import open_output
from pharmacli.pipeline import Cleaner
from pharmacli.protocols import Rule
from pharmacli.reader import CSVR
from pharmacli.registry import RULES
from pharmacli.writer import CSVW

VERSION: Final[str] = "2.0.0"
MAX_SKIPPED: Final[int] = 50

EXIT_OK: Final[int] = 0
EXIT_NOROWS: Final[int] = 1
EXIT_BADIN: Final[int] = 2
EXIT_BADRULE: Final[int] = 3
EXIT_NETWORK: Final[int] = 4


log = logging.getLogger(__name__)

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
    parser.add_argument("--version", action = "version", version = f"PharmaCLI {VERSION}")

    # define output input and dryrun tags
        
    
    # supressing so newer iterations never stop the previous ones
    verbosity = argparse.ArgumentParser(add_help=False)
    verbosity.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="debug logging")

    fileopts = argparse.ArgumentParser(add_help=False)
    fileopts.add_argument("input", type = Path, help = "path to input CSV")
    fileopts.add_argument("-o", "--outfile", type=Path, default=None, help="write to here instead of stdout")
    fileopts.add_argument("-n", "--dryrun", action='store_true', help = "report only, write nothing")



    commands = parser.add_subparsers(dest="command", required= True, metavar="COMMAND")
    # add new rules command
    clean = commands.add_parser("clean", parents=[fileopts, verbosity], help="run one or more rules")
    clean.add_argument("--rule", action="append", default=[], metavar="NAME[:KEY=VALUE, ...]", help="repeatable; avaliable {', '.join(RULES.names())}")


    dedup = commands.add_parser("dedup", parents=[fileopts, verbosity], help= "remove dupes")
    dedup.add_argument("--key", default= "row", choices= ('row', 'ndc', 'ndc+date'), help = "what makes the rows duplicates")
    sortd = commands.add_parser("sort", parents=[fileopts, verbosity], help= "sorts all data by column name")
    sortd.add_argument("--by", default="description",choices = {'description', 'ndc', 'price', 'date'},help = "select the column in which you want to sort the column by, default is by description")



    # list rules and columns 
    commands.add_parser("rules", parents=[verbosity], help="list every registry approved tool")
    commands.add_parser("columns", parents=[verbosity], help="list the expected columns")



    # net commands
    fetch = commands.add_parser("fetch", parents=[verbosity],help="pull NADAC records into SQLite")
    fetch.add_argument("--model", default="asyncio",choices=("sequential", "threaded", "multiproc", "asyncio"),help="which concurrency model to fetch with")
    fetch.add_argument("--workers", type=int, default=None, help="pool size / max requests in flight")
    fetch.add_argument("--limit", type=int, default=5000,help="how many records to fetch")
    fetch.add_argument("--db", default=None, help="SQLite file to write to")
    fetch.add_argument("--fresh", action="store_true",help="delete the database first, so the fetch is timed from empty instead of resuming")

    bench = commands.add_parser("bench", parents=[verbosity],help="time the concurrency models")
    bench.add_argument("--workload", default="both", choices=("io", "cpu", "both"), help="io = network fetch, cpu = parsing, both = run each")
    bench.add_argument("--workers", type=int, default=None)
    bench.add_argument("--limit", type=int, default=2000)
    bench.add_argument("--db", default=None)

    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    return parser


# obtain arguments from CL
def cmdrules() -> int:
    for name, summ in RULES.describeall():
        print(f"{name: <16} {summ}")
    return EXIT_OK

def cmdcol() -> int:
    for pos, col in enumerate(COLUMNS, start=1):
        print(f"{pos:>2}. {col}")
    return EXIT_OK


# rules dictionary, create, and if needed reject
def buildrule(args: argparse.Namespace) -> list[Rule] | None:


    if args.command == "dedup":
        return [RULES.create("dedup", key=args.key)]

    if args.command == "sort":
        return [RULES.create("sort", by=args.by)]

    built: list[Rule] = []
    for raw in args.rule:
        try:
            name, options = parserule(raw)
            built.append(RULES.create(name, **options))
        except RuleError as exc:
            log.error("%s", exc)
            return None
    return built

# fetching model
def cmdfetch(args: argparse.Namespace) -> int:
    from pharmacli.collect import MODELS, PAGE_SIZE
    from pharmacli.store import connect, count, insert, pending_offsets

    config = load()
    db = args.db or config.db
    workers = args.workers or config.workers

    # removed after model fetch
    if args.fresh:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db + suffix)
            except FileNotFoundError:
                pass
        log.info("removed %s", db)

    conn = connect(db)
    try:
        # already stored items are skipped
        pages = pending_offsets(conn, args.limit, PAGE_SIZE)
        if not pages:
            log.info("nothing to fetch, %d rows already stored", count(conn))
            return EXIT_OK

        log.info("fetching %d pages with the %s model (%d workers)",len(pages), args.model, workers)
        model = MODELS[args.model]

        # each page is written after it comes back, not after whole program
        stored = 0

        def on_page(records):
            nonlocal stored
            stored += insert(conn, records)

        start = time.perf_counter()
        try:
            if args.model == "asyncio":
                # event loop for async model
                records = asyncio.run(model(pages, workers, on_page))
            else:
                records = model(pages, workers, on_page)
        except FetchError as exc:
            log.error("%s", exc)
            return EXIT_NETWORK
        except KeyboardInterrupt:
            # pages written stays written
            log.warning("interrupted, run the same command again to carry on")
            return EXIT_NETWORK
        elapsed = time.perf_counter() - start

        log.info("%d records in %.2fs (%.0f rec/s), %d new rows, %d total",
                 len(records), elapsed,
                 len(records) / elapsed if elapsed else 0, stored, count(conn))
    finally:
        conn.close()
    return EXIT_OK


# timing the models using bench.py
def cmdbench(args: argparse.Namespace) -> int:
    from pharmacli.bench import run

    config = load()
    try:
        run(args.workload, args.limit, args.workers or config.workers,
            args.db or config.db)
    except FetchError as exc:
        log.error("%s", exc)
        return EXIT_NETWORK
    return EXIT_OK


def dispatch(args: argparse.Namespace) -> int:
    if args.command == "rules":
        return cmdrules()
    
    if args.command == "columns":
        return cmdcol()

    # do not read csv
    if args.command == "fetch":
        return cmdfetch(args)

    if args.command == "bench":
        return cmdbench(args)

    try:
        report = CSVR(args.input).read()
    except PharmaError as exc:
        log.error("%s", exc)
        return EXIT_BADIN

    log.info("understood %d items, skipped %d", len(report.records), len(report.skipped))
    for row in report.skipped[:MAX_SKIPPED]:
        log.info("%s", row)


    if not report.records:
        log.error("no usable rows found")
        return EXIT_NOROWS

    rules = buildrule(args)
    if rules is None:
        return EXIT_BADRULE

    clean = Cleaner(tuple(rules)).run(report.records)

    for name, removed in clean.per_rule:
        log.info("%s: rows removed: %d", name, removed)
    for note in clean.notes:
        log.info("%s", note)

    outpath = args.outfile or "stdout"
    if args.dryrun:
        log.info("dry run: could write %d rows to %s", len(clean.records), outpath)
        return EXIT_OK

    try:
        with open_output(args.outfile) as stream:
            written = CSVW(stream).write(clean.records)
    except OSError as exc:
        log.error("cannot write to %s: %s", args.outfile, exc)
        return EXIT_BADIN

    log.info("%d rows to %s", written, outpath)
    return EXIT_OK



def main(argv: list[str] | None = None) -> int:
    parser = buildparse()
    args = parser.parse_args(argv)

    # diagnostics go to stderr so stdout stays clean for piped CSV output
    level = "DEBUG" if getattr(args, "verbose", False) else load().log_level
    logging.basicConfig(level=level, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    # httpx logs a line per request at INFO, which buries our own output during a
    # fetch. keep it unless someone actually asked for debug
    if level != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)

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