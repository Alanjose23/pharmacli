import argparse
import asyncio
import math
import logging
import multiprocessing
import os
import sys
import tempfile
import time
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Final

from pharmacli.models.rows import COLUMNS

# import reader, writer, rules, pipeline, models, errors
from pharmacli.config import load
from pharmacli.err import FetchError, PharmaError, RuleError
from pharmacli.output import open_output
from pharmacli.pipeline import clean as run_rules
from pharmacli.protocols import BaseRule
from pharmacli.reader import read_csv
from pharmacli.rules import RULES, describe, make

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
    clean.add_argument("--rule", action="append", default=[], metavar="NAME[:KEY=VALUE, ...]", help=f"repeatable; available: {', '.join(sorted(RULES))}")


    dedup = commands.add_parser("dedup", parents=[fileopts, verbosity], help= "remove dupes")
    dedup.add_argument("--key", default= "row", choices= ('row', 'ndc', 'ndc+date'), help = "what makes the rows duplicates")
    sortd = commands.add_parser("sort", parents=[fileopts, verbosity], help= "sorts all data by column name")
    sortd.add_argument("--by", default="description",choices = {'description', 'ndc', 'price', 'date'},help = "select the column in which you want to sort the column by, default is by description")



    # list rules and columns 
    commands.add_parser("rules", parents=[verbosity], help="list the CSV cleaning rules")
    commands.add_parser("columns", parents=[verbosity], help="list the expected columns")



    # net commands
    fetch = commands.add_parser("fetch", parents=[verbosity],help="pull NADAC records into SQLite")
    fetch.add_argument("--model", default="asyncio",choices=("sequential", "threaded", "multiproc", "asyncio"),help="which concurrency model to fetch with")
    fetch.add_argument("--workers", type=int, default=None, help="pool size / max requests in flight")
    fetch.add_argument("--limit", type=int, default=5000,help="how many records to fetch")
    fetch.add_argument("--db", default=None, help="SQLite file to write to")
    fetch.add_argument("--fresh", action="store_true",help="delete the database first, so the fetch is timed from empty instead of resuming")
    # the API takes up to 5000 rows a request. the 100 default stays because bench
    # needs many small pages to have any concurrency to measure -- a bulk load wants
    # the opposite, so it is a flag rather than a new constant
    fetch.add_argument("--page-size", type=int, default=None, metavar="N", help="records per request, API max 5000 (default 100)")

    # turns an NDC from the panel into an actual drug via openFDA. read-only,
    # no API key -- the NDC Directory is open
    look = commands.add_parser("lookup", parents=[verbosity], help="what drug is this NDC?")
    look.add_argument("ndc", help="11-digit NDC, e.g. 00378351501")
    look.add_argument("--db", default=None, help="SQLite file to read the price from")
    # A flag, not a `history` command. This is enrich.local's query with LIMIT 1
    # removed -- same table, same NDC, same --db -- and "what is this drug" and
    # "what has it been doing" are one question at a terminal. Same call the
    # `margin -o` comment makes above.
    look.add_argument("--history", action="store_true",
                      help="every weekly NADAC price for this NDC, not just the newest")

    # the pharmacy's stock list. `watch` alone lists; add/rm mutate
    watch = commands.add_parser("watch", parents=[verbosity], help="the NDCs your pharmacy stocks")
    watch.add_argument("action", nargs="?", choices=("add", "rm", "import", "packs"),
                       help="omit to list; `import` takes a costs CSV")
    watch.add_argument("ndc", nargs="*", help="NDCs, or a CSV path for `import`")
    # openFDA allows 1,000 requests a day without a key, so filling a 5,000-NDC
    # shelf takes several runs. Cached NDCs are skipped, so re-running resumes.
    watch.add_argument("--limit", type=int, default=200, metavar="N",
                       help="packs: how many NDCs to look up this run (default 200)")
    watch.add_argument("--cost", type=float, default=None,
                       help="your acquisition cost per unit for these NDCs")
    watch.add_argument("--db", default=None)

    reo = commands.add_parser("reorder", parents=[verbosity],
                              help="what to reorder, and when to place the order")
    reo.add_argument("--within", type=int, default=14,
                     help="only show drugs needing an order inside N days (default 14)")
    reo.add_argument("--lead-days", type=int, default=LEAD_DAYS,
                     help="wholesaler lead time in days (default 2)")
    # NADAC reprices everything at once, so in most weeks every drug is the same
    # age and no price move is imminent. Pointing at a mid-cycle file is the only
    # way to see the price clock actually drive a decision.
    reo.add_argument("--as-of", default=None, metavar="YYYY-MM-DD",
                     help="use this NADAC file instead of the newest")
    # same reason as `margin --top`: sorted by order_by, so the head is the
    # part that is actually due. The totals below still count every line.
    reo.add_argument("--top", type=int, default=40, metavar="N",
                     help="show the N most urgent (default 40, 0 for all)")
    reo.add_argument("--db", default=None)

    ask = commands.add_parser("ask", parents=[verbosity],
                              help="ask in English; bare `ask` lists what it answers")
    ask.add_argument("question", nargs="?", default=None,
                     help="the question. Omit it to see every answerable one.")
    # Only the argument the matched question declares is read. A question that
    # needs an NDC prompts for one when stdin is a tty; numeric knobs fall
    # through to the underlying function's own default rather than asking.
    ask.add_argument("--ndc", default=None)
    ask.add_argument("--phrase", default=None, help="for the description lookup")
    # ask.add_argument("--topic", default=None, help="for `why` -- a constant or concept")  # off: model/why questions -- uncomment to restore
    ask.add_argument("-o", "--out", default=None, metavar="FILE",
                     help="write every shelf drug's answers to a CSV, one row per drug")
    ask.add_argument("--db", default=None)

    chk = commands.add_parser("check", parents=[verbosity],
                              help="check an inventory CSV without importing it")
    chk.add_argument("csv", help="columns: ndc,cost[,on_hand,monthly]")
    chk.add_argument("--within", type=int, default=14, metavar="DAYS")
    chk.add_argument("--dead-days", type=int, default=90, metavar="DAYS")
    chk.add_argument("--db", default=None)

    chat = commands.add_parser("chat", parents=[verbosity],
                               help="the same questions, in a browser")
    chat.add_argument("--port", type=int, default=8765)
    chat.add_argument("--no-open", action="store_true", help="do not open a browser")
    chat.add_argument("--db", default=None)

    tod = commands.add_parser("today", parents=[verbosity],
                              help="the three worklists, ranked by money")
    tod.add_argument("--within", type=int, default=21, metavar="N",
                     help="reorder horizon in days (default 21)")
    # 180 days of supply is the usual line for capital that is not turning.
    # Nothing here is 'expired' -- NADAC carries no expiry date, and guessing one
    # would be inventing a clinical fact.
    tod.add_argument("--dead-days", type=int, default=180, metavar="N",
                     help="days of supply above which stock counts as dead (default 180)")
    tod.add_argument("--db", default=None)

    marg = commands.add_parser("margin", parents=[verbosity],
                               help="where your cost sits against the NADAC benchmark")
    # a real inventory is thousands of lines. The ranking already puts the money
    # first, so print the head and say what was left off -- an unbounded dump is
    # the same as no answer for someone standing at a terminal.
    marg.add_argument("--top", type=int, default=25, metavar="N",
                      help="show the worst N lines (default 25, 0 for all)")
    # A MAC appeal is this same table, filed rather than read. It needs no new
    # command: same query, same ranking, same break-even maths -- only the
    # output format differs, and -o is already how every other command writes.
    marg.add_argument("-o", "--outfile", default=None, metavar="FILE",
                      help="write the underwater lines as CSV to file (a MAC appeal pack)")
    marg.add_argument("--db", default=None)

    fc = commands.add_parser("forecast", parents=[verbosity],
                             help="which watched NDCs are likely to reprice next week")
    # 0.25, not 0.5. Decision-curve analysis (model.net_benefit) shows net
    # benefit is positive only across roughly 0.05-0.40 and turns NEGATIVE above
    # ~0.40 -- at p>=0.5 acting on this model is worse than never buying ahead.
    # The default has to sit inside the band the analysis actually supports.
    fc.add_argument("--min-prob", type=float, default=0.25,
                    help="hide predictions below this (net benefit is positive 0.05-0.40)")
    fc.add_argument("--db", default=None)

    sw = commands.add_parser("sweep", parents=[verbosity],
                             help="run the tracked MLflow experiment and promote a winner")
    # --sample bounds the sweep by NDC count, not row count: the panel is only
    # meaningful per-NDC over time, so slicing rows would break every lag feature.
    sw.add_argument("--sample", type=int, default=None, metavar="N",
                    help="sweep on the first N NDCs only (fast smoke run)")
    sw.add_argument("--db", default=None)

    # Shaped like `bench`: a command that DEMONSTRATES something rather than
    # answering a pharmacist's question. --task mirrors --workload.
    nn = commands.add_parser("nn", parents=[verbosity],
                             help="neural networks: backprop check, tabular net, CNN")
    nn.add_argument("--task", default="toy", choices=("toy", "tabular", "xray"),
                    help="toy = hand-derived backprop vs autograd, "
                         "tabular = feed-forward net on the NADAC panel, "
                         "xray = CNN on MedMNIST (an exercise, not a clinical tool)")
    nn.add_argument("--epochs", type=int, default=None, help="override the default")
    nn.add_argument("--sample", type=int, default=None, metavar="N",
                    help="tabular only: use the first N NDCs")
    nn.add_argument("--no-augment", action="store_true", help="xray only: turn augmentation off")
    nn.add_argument("--no-balance", action="store_true",
                    help="xray only: drop the class weighting, to see what it was fixing")
    nn.add_argument("--logdir", default=None, help="TensorBoard run directory")
    nn.add_argument("--json", default=None, metavar="FILE",
                    help="also write the result dict here, so the numbers persist")
    nn.add_argument("--db", default=None)

    # Same shape as `nn` and `bench`: demonstrates something, is not a pharmacy
    # feature. The corpus is nadac.description; no dataset download.
    nlp = commands.add_parser("nlp", parents=[verbosity],
                              help="NLP: TF-IDF bar, a fine-tuned transformer, semantic search")
    nlp.add_argument("--task", default="baseline",
                     choices=("baseline", "finetune", "search"),
                     help="baseline = TF-IDF + logistic regression (the bar), "
                          "finetune = a small transformer against it, "
                          "search = embeddings vs the keyword search `ask` ships")
    nlp.add_argument("--epochs", type=int, default=None)
    nlp.add_argument("--query", action="append", default=[], metavar="TEXT",
                     help="search only: repeatable, overrides the default queries")
    nlp.add_argument("--json", default=None, metavar="FILE",
                     help="also write the result dict here, so the numbers persist")
    nlp.add_argument("--db", default=None)

    bench = commands.add_parser("bench", parents=[verbosity],help="time the concurrency models")
    bench.add_argument("--workload", default="both", choices=("io", "cpu", "numpy", "both"), help="io = network fetch, cpu = parsing, numpy = vectorization, both = run each")
    bench.add_argument("--workers", type=int, default=None)
    bench.add_argument("--limit", type=int, default=2000)
    bench.add_argument("--db", default=None)

    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    return parser


# obtain arguments from CL
def cmdrules() -> int:
    for name, summ in describe():
        print(f"{name: <16} {summ}")
    return EXIT_OK

def cmdcol() -> int:
    for pos, col in enumerate(COLUMNS, start=1):
        print(f"{pos:>2}. {col}")
    return EXIT_OK


# rules dictionary, create, and if needed reject
def buildrule(args: argparse.Namespace) -> list[BaseRule] | None:


    if args.command == "dedup":
        return [make("dedup", key=args.key)]

    if args.command == "sort":
        return [make("sort", by=args.by)]

    built: list[BaseRule] = []
    for raw in args.rule:
        try:
            name, options = parserule(raw)
            built.append(make(name, **options))
        except RuleError as exc:
            log.error("%s", exc)
            return None
    return built

# fetching model
def cmdfetch(args: argparse.Namespace) -> int:
    from pharmacli import collect
    from pharmacli.store import connect, count, insert, pending_offsets

    # global signature for page size
    if args.page_size:
        collect.PAGE_SIZE = args.page_size
    PAGE_SIZE = collect.PAGE_SIZE

    # revised errors so that multiproc would be a viable option with the change in page size
    if (args.page_size and args.model == "multiproc"
            and multiprocessing.get_start_method() != "fork"):
        log.error("--page-size with --model multiproc needs a fork platform "
                  "(this one is %r); use --model asyncio",
                  multiprocessing.get_start_method())
        return EXIT_BADIN

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
        model = collect.MODELS[args.model]

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


# NDC -> drug identity. local panel first (free), then openFDA for the label
# 8 levels, low to high. Unicode because the alternative is a bar chart that
# needs its own line per week; 34 weeks fit on one line this way.
BLOCKS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


def _sparkline(values: list[float]) -> str:
    lo, hi = min(values), max(values)
    if hi == lo:
        return BLOCKS[len(BLOCKS) // 2] * len(values)
    return "".join(BLOCKS[min(len(BLOCKS) - 1,
                              int((v - lo) / (hi - lo) * len(BLOCKS)))]
                   for v in values)


def _print_history(rows: list[tuple[str, float]], unit: str) -> None:
    prices = [p for _, p in rows]
    lo, hi = min(prices), max(prices)
    net = prices[-1] / prices[0] - 1 if prices[0] else 0.0
    moves = [(d, prices[i] / prices[i - 1] - 1)
             for i, (d, _) in enumerate(rows) if i and prices[i] != prices[i - 1]]
    down = sum(1 for _, pct in moves if pct < 0)

    print(f"  history   {len(rows)} weekly files  {rows[0][0]} -> {rows[-1][0]}")
    # a terminal that cannot encode the blocks would raise on print, so ask
    # first and fall back rather than crashing a command that already worked
    try:
        BLOCKS.encode(sys.stdout.encoding or "utf-8")
        print(f"            {_sparkline(prices)}")
    except (UnicodeEncodeError, LookupError):
        pass
    print(f"            high ${hi:.5f}   low ${lo:.5f}   "
          f"net {net:+.1%}   {len(moves)} moves, {down} down")
    if not moves:
        print(f"            never repriced across {len(rows)} files")
        return
    for d, pct in moves:
        print(f"            {d}  {pct:+7.2%}  -> ${dict(rows)[d]:.5f} / {unit}")


def cmdlookup(args: argparse.Namespace) -> int:
    from pharmacli import enrich

    ndc = args.ndc.strip().zfill(11)
    here = enrich.local(ndc, args.db)
    there = enrich.lookup(ndc)

    if here is None and there is None:
        log.error("no record of %s in the panel or openFDA", ndc)
        return EXIT_NOROWS

    print(here["description"] if here else f"NDC {ndc}")
    if there:
        print(f"  generic   {there['generic_name'] or '-'}")
        print(f"  brand     {there['brand_name'] or '-'}")
        print(f"  form      {there['dosage_form'] or '-'}")
        print(f"  route     {there['route'] or '-'}")
        print(f"  labeler   {there['labeler'] or '-'}")
        # what you can actually order. NADAC prices per unit; wholesalers sell packs.
        if there.get("pack_desc"):
            print(f"  pack      {there['pack_desc']}")
    else:
        print("  (not listed in the openFDA NDC Directory)")
    if here:
        print(f"  price     ${here['unit_price']} / {here['pricing_unit']}"
              f"   class {here['rate_class']}   otc {here['otc']}")
        print(f"  effective {here['effective_date']}   as of {here['as_of_date']}")
    else:
        print("  (not priced in the local panel -- run `pharmacli fetch`)")

    if args.history:
        hist = enrich.history(ndc, args.db)
        if len(hist) < 2:
            # one file is not a history, and printing a one-bar sparkline for it
            # would imply there is a trend to read
            print(f"  history   only {len(hist)} weekly file carries this NDC")
        else:
            _print_history(hist, (here or {}).get("pricing_unit", "unit"))
    return EXIT_OK


def _fill_packs(conn, limit: int) -> int:
    from pharmacli import enrich
    from pharmacli.store import packages, save_package, stock, watched

    known = packages(conn)
    # Most urgent first -- fewest days of supply left -- not alphabetical and not
    # by volume. openFDA's 1,000/day budget means a 5,000-line shelf fills over
    # several days, so this ordering decides which drugs are usable meanwhile,
    # and the pack size you need is the one for the order you are about to
    # place. A position with no recorded volume has no shelf clock, so it sorts
    # last rather than first.
    qty = stock(conn)

    def days_left(n: str) -> float:
        oh, mo = qty.get(n, (None, None))
        return oh / (mo / 30.0) if oh is not None and mo else float("inf")

    todo = sorted((n for n in watched(conn) if n not in known), key=days_left)[:limit]
    if not todo:
        print(f"every watched NDC already has a cached pack ({len(known):,} known)")
        return EXIT_OK

    hits = 0
    for i, ndc in enumerate(todo, 1):
        info = enrich.lookup(ndc)
        size = info.get("pack_size") if info else None
        # a miss is cached too, or every run retries the ~3-in-20 that openFDA
        # simply does not list
        save_package(conn, ndc, size, (info or {}).get("pack_desc", ""))
        hits += size is not None
        if i % 25 == 0:
            log.info("%d/%d looked up, %d with a pack size", i, len(todo), hits)
    left = len(watched(conn)) - len(known) - len(todo)
    print(f"{hits} of {len(todo)} resolved to a pack size"
          f"{f', {left:,} still to do -- run again' if left > 0 else ''}")
    return EXIT_OK


def read_inventory(path: str) -> tuple[list[tuple], list[tuple]]:
    import csv

    def num(cell, what):
        cell = cell.strip()
        if not cell:
            return None, None
        try:
            v = float(cell)
        except ValueError:
            return None, f"{what} {cell!r} is not a number"
        return (v, None) if v >= 0 else (None, f"{what} is negative")

    rows, skipped = [], []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for line, raw in enumerate(csv.reader(fh), start=1):
            if not any(c.strip() for c in raw):
                continue                       # blank line, not worth a reason
            if len(raw) < 2:
                skipped.append((line, "needs at least ndc,cost")); continue
            ndc = raw[0].strip()
            if not ndc:
                skipped.append((line, "missing NDC")); continue
            if not ndc.isdigit():
                # the header row lands here, which is the common case and why
                # this is a skip rather than an error
                skipped.append((line, f"NDC {ndc!r} is not digits")); continue

            cost, err = num(raw[1], "cost")
            if err or cost is None:
                skipped.append((line, err or "missing cost")); continue
            oh, err = num(raw[2], "on_hand") if len(raw) > 2 else (None, None)
            if err:
                skipped.append((line, err)); continue
            mo, err = num(raw[3], "monthly") if len(raw) > 3 else (None, None)
            if err:
                skipped.append((line, err)); continue
            rows.append((ndc.zfill(11), cost, oh, mo))
    return rows, skipped


def _import_costs(conn, path: str) -> int:
    from pharmacli.store import watch_cost

    try:
        rows, skipped = read_inventory(path)
    except OSError as exc:
        log.error("cannot read %s: %s", path, exc)
        return EXIT_BADIN

    for line, why in skipped[:MAX_SKIPPED]:
        log.info("line %d: %s", line, why)
    if not rows:
        log.error("no usable ndc,cost rows in %s", path)
        return EXIT_NOROWS

    changed = watch_cost(conn, rows)
    withqty = sum(1 for r in rows if r[2] is not None or r[3] is not None)
    print(f"{len(rows)} lines imported ({changed} rows changed)"
          + (f", {withqty} with quantities" if withqty else "")
          + (f", {len(skipped)} skipped" if skipped else ""))
    return EXIT_OK


# the stock list. pure sqlite3, so this works on a bare install
def cmdwatch(args: argparse.Namespace) -> int:
    from pharmacli.store import (connect, last_priced, newest_week,
                                 watch_add, watch_remove, watched)

    conn = connect(args.db or load().db)
    try:
        if args.action == "add":
            print(f"{watch_add(conn, args.ndc, args.cost)} added "
                  f"({len(watched(conn))} on watch)")
            return EXIT_OK

        if args.action == "packs":
            return _fill_packs(conn, args.limit)

        if args.action == "rm":
            # was MISSING entirely: "rm" is in the action choices and
            # watch_remove was imported, but no branch called it, so
            # `pharmacli watch rm NDC` printed the watch list and exited 0
            # having removed nothing.
            if not args.ndc:
                log.error("usage: pharmacli watch rm <NDC> [<NDC> ...]")
                return EXIT_BADIN
            gone = watch_remove(conn, args.ndc)
            print(f"{gone} removed ({len(watched(conn))} on watch)")
            return EXIT_OK

        if args.action == "import":
            if not args.ndc:
                log.error("usage: pharmacli watch import COSTS.csv")
                return EXIT_BADIN
            return _import_costs(conn, args.ndc[0])

        stock = watched(conn)
        if not stock:
            print("no NDCs on watch. add some: pharmacli watch add <NDC> ...")
            return EXIT_OK
        # flag anything CMS has stopped pricing -- you are stocking a drug that
        # fell out of the file, which is the delisting signal worth acting on
        newest = newest_week(conn)
        marks = last_priced(conn)
        stale = [n for n in stock if marks.get(n) != newest]
        print(f"{len(stock)} NDCs on watch  |  newest NADAC file {newest}")
        for n in stock[:20]:
            flag = "" if marks.get(n) == newest else f"  <- last priced {marks.get(n) or 'never'}"
            print(f"  {n}{flag}")
        if len(stock) > 20:
            print(f"  ... {len(stock) - 20} more")
        if stale:
            print(f"\n{len(stale)} not in the newest file (delisted or never priced)")
    finally:
        conn.close()
    return EXIT_OK


# model-backed repricing alert. needs the [analysis] extra
def _print_due(out, mine, fee, threshold, newest):
    print(f"\nLIKELY TO MOVE >={threshold:.0%} AFTER {newest.date()}")
    print(f"{'NDC':<13} {'drug':<30} {'prob':>5}  {'break-even':>11} -> {'if -5%':>11}")
    print("-" * 82)
    for _, r in out.head(30).iterrows():
        cost = mine.get(r.ndc)
        if cost is None:
            now = later = "no cost"
        else:
            # the model predicts MAGNITUDE, not direction. The move that hurts is
            # NADAC falling -- reimbursement drops while shelf stock was bought
            # at the old price -- so show the downside case explicitly.
            now = breakeven(cost, r.unit_price, fee, r.pricing_unit)
            later = breakeven(cost, r.unit_price * (1 - threshold), fee, r.pricing_unit)
        print(f"{r.ndc:<13} {str(r.description)[:29]:<30} {r.p:>5.2f}  "
              f"{now:>11} -> {later:>11}")


def _print_quiet(live, proba):
    # sort by time-to-due, not probability: in a quiet week every p is ~0.001
    # and ordering by it is noise. 24d is the midpoint of the 21-28 day peak.
    # Past 35d is NOT "overdue" -- those drugs are off the monthly cycle
    # entirely (P(move) 0.045, barely above base rate), and saying overdue
    # sends someone chasing a move that is not coming.
    nxt = live.assign(p=proba, due=lambda x: 24 - x.age_days)
    near = nxt[nxt.age_days <= 35].nsmallest(5, "due")
    offcycle = int((nxt.age_days > 35).sum())
    print("  nothing due -- no watched NDC clears this probability")
    print("\n  nearest candidates (price age -> repricing peaks at 21-28 days):")
    for _, r in near.iterrows():
        when = f"~{r.due / 7:.0f} wk" if r.due > 0 else "due now"
        print(f"    {r.ndc:<13} {str(r.description)[:30]:<31} "
              f"age {r.age_days:>4.0f}d {when:>8}  p={r.p:.3f}")
    if offcycle:
        print(f"    ({offcycle} more are >35d old -- off the monthly cycle, "
              "not overdue)")


def _given(**kw) -> dict:
    return {k: v for k, v in kw.items() if v is not None}


def _dump(args: argparse.Namespace, result: dict) -> None:
    if not getattr(args, "json", None):
        return
    import json as _json
    from pathlib import Path as _Path

    out = _Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_json.dumps(result, indent=1, default=str), encoding="utf-8")
    log.info("wrote %s", out)


def cmdnn(args: argparse.Namespace) -> int:
    try:
        from pharmacli import nn
    except ImportError:
        log.error('nn needs: pip install -e ".[analysis,dl]"')
        return EXIT_BADIN

    if args.task == "toy":
        r = nn.toy(**_given(steps=args.epochs))
        print("\nHAND-DERIVED BACKPROP vs AUTOGRAD")
        print("  gradient max |difference| at step 0, before either has moved:")
        for k, v in r["grad_max_abs_diff"].items():
            print(f"    d{k:<3} {v:.3e}")
        print(f"\n  after {r['steps']} identical SGD steps:")
        print(f"    loss numpy  {r['loss_numpy']:.10f}")
        print(f"    loss torch  {r['loss_torch']:.10f}")
        print(f"    difference  {r['loss_diff']:.3e}")
        print(f"    max weight drift {r['weight_max_abs_diff']:.3e}")
        # float64 machine epsilon is ~2.2e-16; anything at that scale is the
        # same computation in a different order, not a different computation
        _dump(args, r)
        ok = max(r["grad_max_abs_diff"].values()) < 1e-9
        print("\n  AGREE -- the derivation matches autograd" if ok else
              "\n  DISAGREE -- the derivation is wrong")
        return EXIT_OK if ok else EXIT_NOROWS

    if args.task == "tabular":
        r = nn.tabular(**_given(db=args.db, sample=args.sample,
                                epochs=args.epochs, logdir=args.logdir))
        print(f"\nFEED-FORWARD NET on {r['n_features']} preprocessed features")
        print(f"  best val PR-AUC  {r['best_val_pr_auc']:.4f}  "
              f"({len(r['history'])} epochs, early stopped on val PR-AUC)")
        print(f"  test PR-AUC      {r['test']['PR-AUC']:.4f}")
        print(f"  test ROC-AUC     {r['test']['ROC-AUC']:.4f}")
        print("\n  compare: mlflow ui --backend-store-uri sqlite:///mlflow.db")
        print(f"  curves : tensorboard --logdir {r['logdir']}")
        _dump(args, r)
        return EXIT_OK

    r = nn.xray(augment=not args.no_augment, balance=not args.no_balance,
                **_given(epochs=args.epochs, logdir=args.logdir))
    print("\nCNN on MedMNIST -- a deep-learning exercise, NOT a clinical tool")
    print(f"  augmentation     {'on' if r['augmented'] else 'off'}")
    print(f"  class weighting  {'on' if r['balanced'] else 'off'}")
    print(f"  best val accuracy {r['best_val_acc']:.4f}")
    print(f"  test accuracy     {r['test_acc']:.4f}")
    print(f"  test BALANCED acc {r['test_balanced_acc']:.4f}   "
          "<- mean per-class recall; plain accuracy rewards guessing the majority")
    print("\n  per class (this is where it actually fails):")
    for c, v in r["per_class"].items():
        print(f"    class {c}  n={v['n']:<5} recall {v['recall']:.4f}")
    print("\n  confusion (rows = true, cols = predicted):")
    for rowv in r["confusion"]:
        print("    " + "  ".join(f"{v:>5}" for v in rowv))
    print(f"\n  curves: tensorboard --logdir {r['logdir']}")
    _dump(args, r)
    return EXIT_OK


def cmdnlp(args: argparse.Namespace) -> int:
    try:
        from pharmacli import nlp
    except ImportError:
        log.error('nlp needs: pip install -e ".[analysis,nlp]"')
        return EXIT_BADIN

    if args.task == "baseline":
        r = nlp.baseline(args.db)
        print("\nTF-IDF + LOGISTIC REGRESSION -- brand vs generic from the drug name")
        print(f"  {r['n_train']:,} train / {r['n_test']:,} test drugs, "
              f"{r['brand_rate']:.1%} brand. Split by drug, so every test name is unseen.")
        print(f"\n  {'variant':<8}{'bal acc':>9}{'F1':>8}{'precision':>11}{'recall':>8}{'features':>10}")
        for k in ("word", "char"):
            v = r[k]
            print(f"  {k:<8}{v['balanced_accuracy']:>9.4f}{v['F1']:>8.4f}"
                  f"{v['precision']:>11.4f}{v['recall']:>8.4f}{v['features']:>10,}")
        print("\n  always-generic scores 0.5000 balanced. Character n-grams see inside")
        print("  a token (-STATIN, -PRAZOLE, -MAB); word-level cannot, which is why")
        print("  they are the honest bar for a transformer to beat.")
        _dump(args, r)
        return EXIT_OK

    if args.task == "finetune":
        kw = {"db": args.db}
        if args.epochs:
            kw["epochs"] = args.epochs
        r = nlp.finetune(**kw)
        t = r["test"]
        print(f"\nFINE-TUNED {r['model']} ({r['params']/1e6:.0f}M params)")
        print(f"  {r['n_train']:,} train / {r['n_test']:,} test, {len(r['history'])} epochs on CPU")
        print(f"  test balanced accuracy {t['balanced_accuracy']:.4f}  F1 {t['F1']:.4f}")
        print("\n  compare against `pharmacli nlp --task baseline` -- and read the")
        print("  cost, not just the score: seconds of CPU against minutes.")
        _dump(args, r)
        return EXIT_OK

    r = nlp.search(args.db, args.query or None)
    print(f"\nSEMANTIC vs KEYWORD over {r['n']:,} drug descriptions")
    print("  keyword = the algorithm `ask` falls back to without embeddings\n")
    for q, res in r["results"].items():
        print(f"  {q!r}")
        kw = res["keyword"]
        print(f"    keyword   {kw[0] if kw else 'NO RESULTS'}"
              + (f"  (+{len(kw)-1} more)" if len(kw) > 1 else ""))
        for t, sc in res["semantic"][:3]:
            print(f"    semantic  {sc:.3f}  {t}")
        print()
    _dump(args, r)
    return EXIT_OK


def cmdsweep(args: argparse.Namespace) -> int:
    try:
        from pharmacli.track import main
    except ImportError:
        log.error('sweep needs: pip install -e ".[analysis,track]"')
        return EXIT_BADIN
    return main(db=args.db, sample=args.sample)


def cmdforecast(args: argparse.Namespace) -> int:
    try:
        from pharmacli import model
        from pharmacli.frame import load as load_frame   # pandas stays inside the
    except ImportError:                                  # handler: a bare install
        log.error('forecast needs: pip install -e ".[analysis]"')   # must import
        return EXIT_BADIN                                            # cleanly

    from pharmacli.store import connect, costs as watch_costs, watched

    config = load()
    db = args.db or config.db
    conn = connect(db)
    try:
        stock, mine = watched(conn), watch_costs(conn)
    finally:
        conn.close()
    if not stock:
        log.error("no NDCs on watch. add some: pharmacli watch add <NDC> ...")
        return EXIT_NOROWS

    # Week 6 paid off the old refit-every-run comment here: the fit is gone, and
    # the promoted model loads in milliseconds. The fallback below keeps the
    # command working before the first sweep has ever run.
    #
    # This used to be ~30s, and a comment here blamed "load_frame + features
    # over 1M rows". Measurement disagreed: 21.7 of those seconds were two
    # groupby.transform(lambda) calls in model._cycle_features, now vectorized.
    # load+features is ~8s, most of it load_frame's read_sql.
    df = load_frame(db)
    card: dict = {}
    try:
        pipe, card = model.load_promoted(config.model_path)
        log.info("loaded %s v%s (trained %s)", card["name"], card["version"],
                 card["trained_at"][:10])
    except (FileNotFoundError, OSError):
        log.warning("no promoted model at %s -- refitting (~40s). "
                    "run `pharmacli sweep` once to skip this.", config.model_path)
        Xtr, _, ytr, _ = model.temporal_split(model.features(df), cutoff="2100-01-01")
        pipe = model.pipeline().fit(Xtr, ytr)

    # the newest week is unlabelled BY DEFINITION -- its outcome has not happened
    # yet -- which is exactly why it is the week worth scoring
    everything = model.features(df, labeled_only=False)
    newest = everything["as_of_date"].max()
    live = everything[(everything["as_of_date"] == newest)
                      & (everything["ndc"].isin(stock))]
    if live.empty:
        log.error("none of your %d watched NDCs appear in the newest file (%s)",
                  len(stock), newest.date())
        return EXIT_NOROWS

    proba = pipe.predict_proba(live[model.NUMERIC + model.CATEGORICAL])[:, 1]
    out = (live.assign(p=proba)
               .loc[lambda x: x.p >= args.min_prob]
               .sort_values("p", ascending=False))

    _print_due(out, mine, config.dispensing_fee, model.THRESHOLD, newest)
    if out.empty:
        _print_quiet(live, proba)
    if not mine:
        print("\n  no acquisition costs on file -- "
              "pharmacli watch import COSTS.csv for dollar impact")

    print(f"\n{len(out)} of {len(live)} watched NDCs above p={args.min_prob}")
    # was a hardcoded literal copied from a run nobody could reproduce; the
    # sidecar carries the winning run's own measured number.
    print(f"model {card['name']} v{card['version']} test PR-AUC {card['test_pr_auc']:.2f}"
          if card else "model PR-AUC 0.40 vs calendar-only 0.16 (unpromoted refit)")
    return EXIT_OK


# ─────────────────────────────────────────────────────────────────────────────
# PRICE CHANGE MATHS
#
# Everything below rests on one measured fact: CMS reprices NADAC on a MONTHLY
# cycle. Across 1,028,250 rows, 99% of consecutive price moves landed exactly 4
# or 5 weeks apart, and the probability that a price moves next week depends
# almost entirely on how old the current price is.
#
# These are OBSERVED rates from the panel, not estimates. See WEEK5_PLAN.md.
# ─────────────────────────────────────────────────────────────────────────────

# (age of price in days, P(it moves in the coming week)). Read as "up to N days".
MOVE_ODDS: Final[tuple[tuple[int, float], ...]] = (
    (14, 0.002),   # just repriced -- CMS almost never touches it again this soon
    (21, 0.613),   # entering the window
    (28, 0.960),   # THE peak. a price this old almost always moves next week
    (35, 0.002),   # it already moved and reset; this bucket is the far side
    (10**6, 0.045),  # off-cycle drugs -- they do not follow the monthly rhythm
)

# Expected SIGNED change in price, given that a move happens, per rate class.
#
# This replaced a single MEDIAN_MOVE = 0.0174 applied to every drug, plus a rule
# that generics fall and brands rise. Measured over 227,638 real moves in the
# panel, that rule was wrong on both counts:
#
#   class   moves down   E[change | move]   old model    overstated by
#   G          61.4%         -0.681%         -1.740%          2.6x
#   B          53.3%         +0.005%         +1.740%        362x
#   B-ANDA     47.6%         +0.353%         +1.740%          4.9x
#   B-BIO      51.6%         +0.039%         +1.740%         44x
#
# Brands are the headline error. Brand up-moves and down-moves are almost the
# same size and almost equally likely, so they very nearly cancel -- a brand's
# expected weekly change is essentially zero, not a 1.74% rise. On a $346
# specialty brand the old maths showed ~$5.80/unit of expected movement where
# the truth is about half a cent.
#
# One number per class, already probability-weighted, so expected_price needs no
# separate sign rule. Derived by frame.move_profile(); re-run it to re-check.
MOVE_EV: Final[dict[str, float]] = {
    "G":      -0.006806,   # generics erode: more ANDA entrants, lower survey price
    "B":       0.000048,   # brand: up and down cancel. Effectively flat.
    "B-ANDA":  0.003532,   # authorised generic -- the one brand class that moves
    "B-BIO":   0.000393,
}

# How often a move is DOWNWARD, per class. Not used by the maths (MOVE_EV
# already carries it) -- this is shown to the pharmacist, because "generics fall
# 61% of the time" is a fact you can act on and "-0.68%" is not.
DOWN_SHARE: Final[dict[str, float]] = {
    "G": 0.614, "B": 0.533, "B-ANDA": 0.476, "B-BIO": 0.516,
}

# Midpoint of the 22-28 day repricing peak. Used to answer "how many days until
# this price is likely to change".
CYCLE_PEAK: Final[int] = 24


# ─────────────────────────────────────────────────────────────────────────────
# THE REPRICING CALENDAR
#
# 8 of 33 files in the panel carry 11.7-17.9% of NDCs moving >=5%; the other 25
# carry 0.00-0.45%. So "will prices move next week" is not a forecasting problem
# -- it is a date, and CMS publishes it in advance: the NADAC Monthly file lands
# on the first Monday on or after the 15th, and the weekly file carrying it is
# dated that Wednesday.
#
# That rule reproduces ALL 8 observed repricing files in the panel exactly, with
# no fitting and no model. What the trained model is genuinely for is the
# remaining question -- WHICH drugs move inside that file -- where it lifts
# precision from the calendar's 0.151 to 0.417.
# ─────────────────────────────────────────────────────────────────────────────

def _monthly_file(year: int, month: int) -> _date:
    d = _date(year, month, 15)
    d += timedelta(days=(7 - d.weekday()) % 7)   # first Monday on or after the 15th
    return d + timedelta(days=2)                 # the Wednesday file that carries it


def next_reprice(after: _date) -> _date:
    d = _monthly_file(after.year, after.month)
    if d <= after:                               # this month's has already landed
        y, m = (after.year + 1, 1) if after.month == 12 else (after.year, after.month + 1)
        d = _monthly_file(y, m)
    return d


def move_odds(age_days: float) -> float:
    for limit, odds in MOVE_ODDS:
        if age_days <= limit:
            return odds
    return MOVE_ODDS[-1][1]


def expected_price(price: float, age_days: float, rate_class: str) -> float:
    # an unrecognised class is treated as plain brand: the most conservative
    # option, since B is the flattest and will not invent movement that is not there
    ev = MOVE_EV.get(rate_class, MOVE_EV["B"])
    return price * (1 + ev * move_odds(age_days))


def days_to_reprice(age_days: float) -> float:
    return max(0.0, CYCLE_PEAK - age_days)


# how long the wholesaler takes. Most community pharmacies get next-day
# delivery on a daily order cycle; two days is the safe planning assumption.
LEAD_DAYS: Final[int] = 2

# Smallest dollar swing worth putting in front of a pharmacist as a REASON to
# move an order. Measured on 2026-07-08, a peak repricing week where 4,808 of
# 4,918 shelf positions had a likely move: median swing $0.09, p95 $0.69, max
# $4.08, and $880 across the whole shelf.
#
# The dispensing fee ($10.64) was the obvious candidate and is wrong -- it
# silences every row, even on a peak week. It would be the right bar if acting
# cost something, but re-timing an order you are placing anyway costs nothing.
# So the bar is only "loud enough not to be noise". A dollar admits ~109
# positions on a peak week and rejects the $0.26 advice that reading a raw
# swing produces.
#
# The honest headline is that per-drug timing is worth CENTS; the money is the
# aggregate, which the worklist already totals.
MATERIAL_SWING: Final[float] = 1.00


def reorder_advice(on_hand: float, monthly: float, age_days: float,
                   price: float, rate_class: str,
                   lead_days: int = LEAD_DAYS, min_swing: float = 0.0) -> dict:
    daily = monthly / 30.0 if monthly else 0.0
    if daily <= 0:
        return {"action": "no data", "why": "no dispensing volume recorded",
                "days_left": None, "order_by": None, "qty": 0, "saving": 0.0}

    days_left = on_hand / daily
    order_by = days_left - lead_days
    to_reprice = days_to_reprice(age_days)
    qty = round(monthly)                       # one month of demand
    projected = expected_price(price, age_days, rate_class)
    swing = (projected - price) * qty          # dollars, over the order quantity

    # A move has to be LIKELY before it is worth bending the order around.
    # Without this gate a drug that just repriced (odds 0.002) would be told to
    # wait 17 days for a saving of nothing -- the schedule says a move is coming
    # eventually, but not soon enough to plan against.
    likely = move_odds(age_days) >= 0.5

    # ...and it has to be worth MONEY, not just likely. Correcting the per-class
    # move sizes shrank brand swings by ~362x, which exposed this gap: without a
    # floor the advice became "order now to capture $0.26" on 1,800 units, a
    # decision no pharmacist should be asked to make. See MATERIAL_SWING for the
    # measurement behind the default.
    material = abs(swing) >= min_swing

    if order_by <= 0:
        action, why, act_on_price = "order now", "at or past stock-out", False
    elif likely and material and swing < 0 and to_reprice < order_by:
        # price expected to fall and there is room on the shelf to wait for it.
        # to_reprice == 0 means the drop lands in THIS week's file -- "wait 0d"
        # reads as a bug, so say what it means: hold, the new price is imminent.
        action = "hold" if to_reprice < 1 else f"wait {to_reprice:.0f}d"
        # WRONG TENSE, fixed: this used to read "drop lands in this week's file".
        # The file on screen IS this week's, so that sentence claimed the drop was
        # already in the price shown -- while still advising a hold, which is a
        # contradiction. move_odds is P(moves in the COMING week), and
        # to_reprice == 0 means the cycle peak has passed, so the move is OVERDUE
        # and lands in the NEXT file. Say that instead.
        why = (f"reprice overdue, {move_odds(age_days):.0%} chance in next week's file"
               if to_reprice < 1 else
               f"generic erosion due in {to_reprice:.0f}d")
        why += f", {order_by:.0f}d of slack"
        act_on_price = True
    elif likely and material and swing > 0 and to_reprice < order_by:
        # price expected to rise before you would otherwise have ordered
        action, why, act_on_price = "order now", f"brand rise likely in {to_reprice:.0f}d", True
    else:
        action = f"in {order_by:.0f}d"
        why = "routine -- no likely move inside the window"
        act_on_price = False

    # saving is only meaningful when the timing was actually changed for price
    return {"action": action, "why": why, "days_left": days_left,
            "order_by": order_by, "qty": qty,
            "saving": abs(swing) if act_on_price else 0.0}


def breakeven(cost: float, nadac: float, fee: float, unit: str) -> str:
    gap = cost - nadac
    if gap <= 0:
        return "never"
    qty = fee / gap
    # under one unit means the gap alone eats the whole fee: every script loses
    # money whatever its size. "0 ML" reads as a rounding bug, so say what it is.
    return f"{qty:,.0f} {unit}" if qty >= 1 else "ANY qty"


# The index. A pharmacist does not ask "when will this price move", they ask
# "what do I do today" -- and the answer is spread across three commands they
# would have to know to run. This ranks the three by MONEY, because that is the
# order they should be worked in, and points at the command that does each one.
#
# Pure sqlite3: no pandas, no model. The two-dependency install gets this.
# The three worklists, as DATA. Split out of cmdtoday so `ask` answers the same
# three questions from the same arithmetic -- the CLI and the question menu
# cannot drift, because they share one computation and one formatter.
def worklists(db: str, within: int = 14, dead_days: int = 90,
              as_of: str | None = None, lead_days: int = LEAD_DAYS,
              shelf: tuple[dict, dict] | None = None) -> dict:
    from pharmacli.store import connect, costs, last_priced, newest_week, stock

    conn = connect(db)
    try:
        week = as_of or newest_week(conn)
        if week is None:
            return {"error": "no NADAC data. pharmacli fetch"}
        if as_of and not conn.execute(
                "SELECT 1 FROM nadac WHERE as_of_date=? LIMIT 1", (as_of,)).fetchone():
            have = [w for (w,) in conn.execute(
                "SELECT DISTINCT as_of_date FROM nadac"
                " ORDER BY as_of_date DESC LIMIT 6")]
            return {"error": f"no NADAC file for {as_of}. recent: {', '.join(have)}"}
        mine, qty = shelf if shelf else (costs(conn), stock(conn))
        marks = last_priced(conn)
        if not mine:
            return {"error": "no inventory recorded. "
                             "pharmacli watch import data/pharmacy_inventory.csv"}
        rows = conn.execute(
            "SELECT ndc, description, CAST(unit_price AS REAL), pricing_unit,"
            " rate_class, CAST(julianday(as_of_date)-julianday(effective_date) AS INT)"
            " FROM nadac WHERE as_of_date = ? AND ndc IN (%s)"
            % ",".join("?" * len(mine)), [week, *mine]).fetchall()
    finally:
        conn.close()

    under, buy, dead = [], [], []
    for ndc, desc, nadac, unit, rc, age in rows:
        oh, mo = qty.get(ndc, (None, None))
        gap = mine[ndc] - nadac
        if gap > 0:
            under.append((gap * (mo or 0), ndc, desc, gap, unit))
        if oh is not None and mo:
            a = reorder_advice(oh, mo, age, mine[ndc], rc, lead_days,
                               min_swing=MATERIAL_SWING)
            if a["order_by"] is not None and a["order_by"] <= within:
                buy.append((a, ndc, desc, unit, age))
            if oh / (mo / 30.0) >= dead_days:
                dead.append((oh * mine[ndc], ndc, desc, oh / (mo / 30.0)))
    # you hold stock in a drug CMS has stopped pricing
    gone = [n for n in mine if marks.get(n) != week and (qty.get(n) or (0,))[0]]
    # ...and NDCs with no row in THIS file at all. For `today` these overlap with
    # `gone`; for a CSV of unknown drugs it is the whole point of the check.
    unpriced = sorted(set(mine) - {r[0] for r in rows})

    # The calendar leads, because on 25 of 33 files the honest answer to "what
    # moves next week" is "nothing, and here is the date when something will".
    nxt = next_reprice(_date.fromisoformat(week))
    return {"week": week, "priced": len(mine), "under": under, "buy": buy,
            "dead": dead, "gone": gone, "unpriced": unpriced,
            "bleed": sum(x[0] for x in under),
            "timing": sum(a["saving"] for a, *_ in buy),
            "capital": sum(x[0] for x in dead),
            "next_reprice": nxt,
            "days_to_reprice": (nxt - _date.fromisoformat(week)).days}


# cmdtoday's print block with `print(` removed. Kept as a list of lines so
# `ask` can reuse the exact wording without re-implementing the formatting --
# if this and worklists() ever disagree, test_ask catches it.
def format_worklists(w: dict, within: int = 14, dead_days: int = 90) -> list[str]:
    dtr = w["days_to_reprice"]
    out = ["", f"{w['week']} NADAC file  ·  {w['priced']:,} shelf positions"]
    if dtr > 7:
        out.append(f"next repricing file {w['next_reprice']} ({dtr} days away)"
                   " -- prices hold until then")
    else:
        out.append(f"next repricing file {w['next_reprice']} ({dtr} days away)"
                   " -- THIS is the week")
    out.append("")

    # APPEAL first: measured at 8x the entire timing play on this inventory.
    if w["under"]:
        out.append(f"APPEAL   {len(w['under']):,} positions cost more than NADAC"
                   f"{'':>8}${w['bleed']:,.2f}/month")
        for _, ndc, desc, gap, unit in sorted(w["under"], reverse=True)[:3]:
            out.append(f"         {str(desc)[:34]:<35} +${gap:.5f}/{unit}")
        out.append("         pharmacli margin -o appeal.csv")
    else:
        out.append("APPEAL   nothing underwater -- every recorded cost is at or below NADAC")

    out.append("")
    if w["buy"]:
        out.append(f"BUY      {len(w['buy']):,} of {w['priced']:,} need an order"
                   f" inside {within} days{'':>6}${w['timing']:,.2f} timing value")
        for a, ndc, desc, _, _ in sorted(w["buy"], key=lambda x: x[0]["order_by"])[:3]:
            out.append(f"         {str(desc)[:34]:<35} {a['action']}")
        out.append("         pharmacli reorder")
    else:
        out.append(f"BUY      nothing needs ordering inside {within} days")

    out.append("")
    if w["dead"] or w["gone"]:
        if w["dead"]:
            out.append(f"CLEAR    {len(w['dead']):,} positions over {dead_days}d of supply"
                       f"{'':>10}${w['capital']:,.2f} tied up")
            for _, ndc, desc, days in sorted(w["dead"], reverse=True)[:3]:
                out.append(f"         {str(desc)[:34]:<35} {days:,.0f} days of supply")
        if w["gone"]:
            out.append(f"         {len(w['gone']):,} stocked NDCs are no longer priced by CMS")
    else:
        # an empty result is an ANSWER. Say which two checks came back clean.
        out.append(f"CLEAR    nothing dead -- every position turns inside {dead_days} days,")
        out.append("         and every stocked NDC is still in the newest file")
    return out


def cmdtoday(args: argparse.Namespace) -> int:
    w = worklists(args.db or load().db, args.within, args.dead_days)
    if "error" in w:
        log.error("%s", w["error"])
        return EXIT_NOROWS
    print("\n".join(format_worklists(w, args.within, args.dead_days)))
    return EXIT_OK


# where your acquisition cost sits against the national benchmark. This is the
# number a MAC appeal is argued with, and it needs no dispensed quantity.
def cmdmargin(args: argparse.Namespace) -> int:
    from pharmacli.store import connect, costs, newest_week, stock

    config = load()
    conn = connect(args.db or config.db)
    try:
        mine = costs(conn)
        qty = stock(conn)
        if not mine:
            log.error("no acquisition costs recorded. "
                      "pharmacli watch import COSTS.csv  (columns: ndc,cost)")
            return EXIT_NOROWS
        # hoisted out of the query: an appeal argued from an unnamed file is
        # unarguable, so every exported row has to carry the date it came from
        week = newest_week(conn)
        rows = conn.execute(
            "SELECT ndc, description, CAST(unit_price AS REAL), pricing_unit"
            " FROM nadac WHERE as_of_date = ?"
            " AND ndc IN (%s)" % ",".join("?" * len(mine)),
            [week, *mine]).fetchall()
    finally:
        conn.close()

    fee = config.dispensing_fee

    def monthly_exposure(ndc, nadac):
        _, mo = qty.get(ndc, (None, None))
        return None if mo is None else (mine[ndc] - nadac) * mo

    have_vol = any(monthly_exposure(r[0], r[2]) is not None for r in rows)
    # rank by dollars where volume is known, per-unit gap where it is not
    rows = sorted(rows, reverse=True,
                  key=lambda r: (monthly_exposure(r[0], r[2]) if have_vol
                                 else mine[r[0]] - r[2]) or 0.0)

    if args.outfile:
        # Only the lines that are actually underwater. You do not appeal a drug
        # you are making money on, and a pack padded with them invites the PBM
        # to reject the whole thing.
        import csv

        from pharmacli.output import open_output

        under_rows = [r for r in rows if mine[r[0]] - r[2] > 0]
        with open_output(Path(args.outfile)) as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(["ndc", "description", "nadac_file", "nadac_unit", "your_cost",
                        "gap_per_unit", "breakeven_qty", "monthly_units", "monthly_loss"])
            for ndc, desc, nadac, unit in under_rows:
                _, mo = qty.get(ndc, (None, None))
                exp = monthly_exposure(ndc, nadac)
                w.writerow([ndc, desc, week, f"{nadac:.5f}", f"{mine[ndc]:.5f}",
                            f"{mine[ndc] - nadac:.5f}",
                            breakeven(mine[ndc], nadac, fee, unit),
                            "" if mo is None else f"{mo:g}",
                            "" if exp is None else f"{exp:.2f}"])
        total = sum(e for e in (monthly_exposure(r[0], r[2]) for r in under_rows)
                    if e is not None)
        log.info("%d underwater lines -> %s (%s/month at your recorded volumes)",
                 len(under_rows), args.outfile, f"${total:,.2f}")
        return EXIT_OK

    print(f"\nYOUR COST vs NADAC   (dispensing fee ${fee:.2f})")
    hdr = f"{'NDC':<13} {'drug':<30} {'you':>9} {'NADAC':>9} {'gap/unit':>9} {'break-even':>11}"
    print(hdr + (f" {'$/month':>9}" if have_vol else ""))
    print("-" * (len(hdr) + (10 if have_vol else 0)))

    # totals are computed over every line; only the head is printed
    limit = args.top or len(rows)
    under = bleed = 0
    for i, (ndc, desc, nadac, unit) in enumerate(rows):
        gap = mine[ndc] - nadac
        under += gap > 0
        exp = monthly_exposure(ndc, nadac)
        if exp and exp > 0:
            bleed += exp
        if i >= limit:
            continue                             # ranked out of the printed head
        tail = ""
        if have_vol:
            tail = f" {'':>9}" if exp is None else f" {exp:>+9.2f}"
        print(f"{ndc:<13} {str(desc)[:29]:<30} {mine[ndc]:>9.5f} {nadac:>9.5f} "
              f"{gap:>+9.5f} {breakeven(mine[ndc], nadac, fee, unit):>11}{tail}")

    if limit < len(rows):
        print(f"... {len(rows) - limit:,} more lines not shown (--top 0 for all)")
    print(f"\n{under} of {len(rows)} cost more than NADAC per unit.")
    if have_vol:
        print(f"costing you ${bleed:,.2f}/month at your recorded dispensing volume.")
    else:
        print("add on_hand and monthly columns to your import for dollar totals.")
    print("break-even = units per script at which the dispensing fee stops "
          "covering the gap.")
    return EXIT_OK


# the reorder worklist: shelf clock and price clock together
def cmdreorder(args: argparse.Namespace) -> int:
    from pharmacli.store import connect, packages

    config = load()
    db = args.db or config.db
    # the shelf-and-price arithmetic is worklists(); this command adds the pack
    # rounding and the wide table, nothing else
    w = worklists(db, args.within, as_of=args.as_of, lead_days=args.lead_days)
    if "error" in w:
        log.error("%s", w["error"])
        return EXIT_BADIN if args.as_of else EXIT_NOROWS

    conn = connect(db)
    try:
        packs = packages(conn)
    finally:
        conn.close()

    plan, week = list(w["buy"]), w["week"]
    if not plan:
        print(f"nothing needs ordering inside {args.within} days.")
        return EXIT_OK

    plan.sort(key=lambda x: x[0]["order_by"])
    print(f"\nREORDER  (NADAC file {week}, {args.lead_days}-day lead time)")
    print(f"{'drug':<31} {'on hand':>9} {'order':>10} {'qty':>13} {'saves':>8}  why")
    print("-" * 104)
    saved = 0.0
    limit = args.top or len(plan)
    for i, (a, ndc, desc, unit, age) in enumerate(plan):
        saved += a["saving"]                     # total spans every line
        if i >= limit:
            continue
        sv = f"${a['saving']:,.2f}" if a["saving"] else "-"
        # NADAC prices per unit, wholesalers sell packs. Where openFDA gave us a
        # pack size, round UP to whole packs -- you cannot order 1.7 bottles.
        # Unresolved NDCs keep raw units rather than a guessed pack.
        size = packs.get(ndc, (None, ""))[0]
        qtytxt = (f"{math.ceil(a['qty'] / size):,}x{size:,.0f}" if size
                  else f"{a['qty']:,}")
        print(f"{str(desc)[:30]:<31} {a['days_left']:>6.0f}d   {a['action']:>10} "
              f"{qtytxt:>13} {sv:>8}  {a['why']}")
    if limit < len(plan):
        print(f"... {len(plan) - limit:,} more due in this window (--top 0 for all)")
    print(f"\n{len(plan)} of {w['priced']:,} stocked drugs need an order inside "
          f"{args.within} days.")
    if saved:
        # RELABELLED, not recalculated. This is median_move x P(move) x order
        # value -- an EXPECTATION over many buys, not cash you can bank on this
        # order. Calling it "worth $X" invited reading it as a realised saving,
        # which on a shelf where every drug is mid-cycle overstates what timing
        # can actually do. The number is unchanged; the claim it makes is not.
        print(f"expected price movement on these orders: ${saved:,.2f} "
              f"(an expectation across many buys, not a banked saving).")
    return EXIT_OK


# Ask a question instead of knowing a subcommand. The registry, the matcher
# and every answer live in ask.py; this is the argparse shim.
# Check a CSV without adopting it. `watch import` writes the file into your
# saved shelf and every later command then reports on all 5,000 rows; this
# answers "what about THESE drugs" for a file someone just handed you.
# To keep the rows afterwards, run `watch import` on the same file -- there is
# no --save flag here because that command already is one.
def cmdcheck(args: argparse.Namespace) -> int:
    try:
        rows, skipped = read_inventory(args.csv)
    except OSError as exc:
        log.error("cannot read %s: %s", args.csv, exc)
        return EXIT_BADIN
    for line, why in skipped[:MAX_SKIPPED]:
        log.info("line %d: %s", line, why)
    if not rows:
        log.error("no usable ndc,cost rows in %s", args.csv)
        return EXIT_NOROWS

    # the same arithmetic `today` runs, pointed at this file instead of the DB
    shelf = ({n: c for n, c, _, _ in rows}, {n: (oh, mo) for n, _, oh, mo in rows})
    w = worklists(args.db or load().db, args.within, args.dead_days, shelf=shelf)
    if "error" in w:
        log.error("%s", w["error"])
        return EXIT_NOROWS

    print(f"\n{args.csv}  ·  {len(rows):,} lines"
          + (f", {len(skipped)} skipped" if skipped else ""))
    print("\n".join(format_worklists(w, args.within, args.dead_days)))
    if w["unpriced"]:
        print(f"\nNOT PRICED  {len(w['unpriced']):,} of {len(rows):,} NDCs have no row "
              f"in the {w['week']} file")
        for n in w["unpriced"][:5]:
            print(f"         {n}")
    return EXIT_OK


def cmdchat(args: argparse.Namespace) -> int:
    from pharmacli.chat import serve

    serve(args.db, args.port, not args.no_open)
    return EXIT_OK


def cmdask(args: argparse.Namespace) -> int:
    from pharmacli import ask as _ask

    if args.out:
        try:
            n = _ask.write_drugs(args.out, args.db)
        except OSError as exc:
            log.error("cannot write %s: %s", args.out, exc)
            return EXIT_BADIN
        print(f"{n:,} drugs -> {args.out}")
        return EXIT_OK
    if not args.question:
        print("\n".join(_ask.menu()))
        return EXIT_OK
    # only the arg the matched question declares is used; the rest are dropped
    kw = {k: v for k, v in
          ((n, getattr(args, n, None)) for n in
           ("ndc", "phrase"))    # "topic" # off: model/why questions -- uncomment to restore
          if v is not None}
    label, lines = _ask.answer(args.question, db=args.db, **kw)
    print("\n".join([f"· {label}", ""] + lines if label else lines))
    return EXIT_OK


# timing the models using bench.py
def cmdbench(args: argparse.Namespace) -> int:
    from pharmacli.bench import run

    config = load()

    # revised destroyed data, instead passed working database, or a gathered file
    bench_db = args.db or str(Path(tempfile.gettempdir()) / "pharmacli-bench.db")
    try:
        run(args.workload, args.limit, args.workers or config.workers, bench_db)
    except FetchError as exc:
        log.error("%s", exc)
        return EXIT_NETWORK
    return EXIT_OK


# commands that do NOT read an input CSV. Was nine near-identical
# `if args.command == "x": return cmdx(args)` blocks; a dict says the same thing
# and a new command is now one line here instead of three.
HANDLERS = {
    "rules":    lambda a: cmdrules(),
    "columns":  lambda a: cmdcol(),
    "fetch":    cmdfetch,
    "bench":    cmdbench,
    "lookup":   cmdlookup,
    "watch":    cmdwatch,
    "forecast": cmdforecast,
    "margin":   cmdmargin,
    "reorder":  cmdreorder,
    "ask":      cmdask,
    "chat":     cmdchat,
    "check":    cmdcheck,
    "sweep":    cmdsweep,
    "nn":       cmdnn,
    "today":    cmdtoday,
    "nlp":      cmdnlp,
}


def dispatch(args: argparse.Namespace) -> int:
    if args.command in HANDLERS:
        return HANDLERS[args.command](args)

    # everything below here reads the input CSV: clean, dedup, sort
    try:
        report = read_csv(args.input)
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

    kept, per_rule = run_rules(rules, report.records)

    for name, removed in per_rule:
        log.info("%s: rows removed: %d", name, removed)

    outpath = args.outfile or "stdout"
    if args.dryrun:
        log.info("dry run: could write %d rows to %s", len(kept), outpath)
        return EXIT_OK

    try:
        # was writer.write_csv: an 18-line module around these three lines, with
        # this as its only caller
        import csv
        with open_output(args.outfile) as stream:
            out = csv.writer(stream, lineterminator="\n")
            out.writerow(COLUMNS)
            out.writerows(record.torow() for record in kept)
            written = len(kept)
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


