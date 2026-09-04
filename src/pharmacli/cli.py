import argparse
import asyncio
import logging
import multiprocessing
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Final

from pharmacli.models.rows import COLUMNS

# import reader,writer, registry, pipeline, registry, models, errors
from pharmacli.config import load
from pharmacli.err import FetchError, PharmaError, RuleError
from pharmacli.output import open_output
from pharmacli.pipeline import Cleaner
from pharmacli.protocols import BaseRule
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
    clean.add_argument("--rule", action="append", default=[], metavar="NAME[:KEY=VALUE, ...]", help=f"repeatable; available: {', '.join(RULES.names())}")


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
    # the API takes up to 5000 rows a request. the 100 default stays because bench
    # needs many small pages to have any concurrency to measure -- a bulk load wants
    # the opposite, so it is a flag rather than a new constant
    fetch.add_argument("--page-size", type=int, default=None, metavar="N", help="records per request, API max 5000 (default 100)")

    # turns an NDC from the panel into an actual drug via openFDA. read-only,
    # no API key -- the NDC Directory is open
    look = commands.add_parser("lookup", parents=[verbosity], help="what drug is this NDC?")
    look.add_argument("ndc", help="11-digit NDC, e.g. 00378351501")
    look.add_argument("--db", default=None, help="SQLite file to read the price from")

    # the pharmacy's stock list. `watch` alone lists; add/rm mutate
    watch = commands.add_parser("watch", parents=[verbosity], help="the NDCs your pharmacy stocks")
    watch.add_argument("action", nargs="?", choices=("add", "rm", "import"),
                       help="omit to list; `import` takes a costs CSV")
    watch.add_argument("ndc", nargs="*", help="NDCs, or a CSV path for `import`")
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

    desk = commands.add_parser("desk", parents=[verbosity],
                               help="build the buying desk as a single HTML page")
    desk.add_argument("-o", "--outfile", default=None, metavar="FILE",
                      help="where to write (default desk.html)")
    # above the ~5,000 a pharmacy stocks, so search still reaches drugs you do
    # NOT carry -- that is the "should I start stocking this" lookup. Your own
    # shelf is always included regardless of this number.
    desk.add_argument("--limit", type=int, default=8000, metavar="N",
                      help="how many NDCs to inline for search (default 8000)")
    desk.add_argument("--as-of", default=None, metavar="YYYY-MM-DD",
                      help="use this NADAC file instead of the newest")
    desk.add_argument("--pharmacy", default="Your pharmacy",
                      help="name shown in the masthead")
    desk.add_argument("--db", default=None)

    marg = commands.add_parser("margin", parents=[verbosity],
                               help="where your cost sits against the NADAC benchmark")
    # a real inventory is thousands of lines. The ranking already puts the money
    # first, so print the head and say what was left off -- an unbounded dump is
    # the same as no answer for someone standing at a terminal.
    marg.add_argument("--top", type=int, default=25, metavar="N",
                      help="show the worst N lines (default 25, 0 for all)")
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

    bench = commands.add_parser("bench", parents=[verbosity],help="time the concurrency models")
    bench.add_argument("--workload", default="both", choices=("io", "cpu", "numpy", "both"), help="io = network fetch, cpu = parsing, numpy = vectorization, both = run each")
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
def buildrule(args: argparse.Namespace) -> list[BaseRule] | None:


    if args.command == "dedup":
        return [RULES.create("dedup", key=args.key)]

    if args.command == "sort":
        return [RULES.create("sort", by=args.by)]

    built: list[BaseRule] = []
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
    else:
        print("  (not listed in the openFDA NDC Directory)")
    if here:
        print(f"  price     ${here['unit_price']} / {here['pricing_unit']}"
              f"   class {here['rate_class']}   otc {here['otc']}")
        print(f"  effective {here['effective_date']}   as of {here['as_of_date']}")
    else:
        print("  (not priced in the local panel -- run `pharmacli fetch`)")
    return EXIT_OK


def _import_costs(conn, path: str) -> int:
    """Load a pharmacy inventory CSV into the watch list.

    Columns: ndc, cost, [on_hand], [monthly]. The last two are optional, so a
    plain two-column costs file still imports -- and because the upsert
    COALESCEs, a costs-only file never wipes quantities already recorded.

    Bad rows are SKIPPED WITH A REASON rather than failing the file, matching
    how the parse layer treats a bad NADAC row. One typo in a 200-line inventory
    export should cost you that line, not the import.

    stdlib csv on purpose: `watch` must work on a bare three-dependency install.
    """
    import csv

    from pharmacli.store import watch_cost

    def num(cell, what):
        """Parse an optional numeric cell. Returns (value, error-or-None)."""
        cell = cell.strip()
        if not cell:
            return None, None
        try:
            v = float(cell)
        except ValueError:
            return None, f"{what} {cell!r} is not a number"
        return (v, None) if v >= 0 else (None, f"{what} is negative")

    rows, skipped = [], []
    try:
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
                rows.append((ndc, cost, oh, mo))
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
    from pharmacli.store import (connect, newest_week, watch_add,
                             watch_remove, watched)

    conn = connect(args.db or load().db)
    try:
        if args.action == "add":
            print(f"{watch_add(conn, args.ndc, args.cost)} added "
                  f"({len(watched(conn))} on watch)")
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
        marks = {n: d for n, d in conn.execute(
            "SELECT ndc, MAX(as_of_date) FROM nadac GROUP BY ndc")}
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
    """The table when something IS due. Split out of cmdforecast, which was 100
    lines doing four jobs -- this is presentation only, no orchestration."""
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
    """The table when NOTHING is due -- which is most weeks, because NADAC
    reprices monthly. An empty result is an ANSWER, not a failure, so show where
    the stock sits in the cycle instead of printing a blank."""
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

    # ponytail: refits on every run (~40s on the full panel). A forward-buy call
    # is a weekly decision, so a model cache is not worth its invalidation story
    # yet -- joblib.dump the fitted pipeline if this ever gets run hourly.
    log.info("training on the full panel (this takes ~40s)")
    df = load_frame(db)
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
    print("model PR-AUC 0.40 vs calendar-only 0.16 (see WEEK5_PLAN.md)")
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

# Median size of a move, given that one happens. Half of all moves are smaller
# than this, half larger; the distribution is heavily right-skewed, so the mean
# would overstate the typical case.
MEDIAN_MOVE: Final[float] = 0.0174          # 1.74%

# Midpoint of the 22-28 day repricing peak. Used to answer "how many days until
# this price is likely to change".
CYCLE_PEAK: Final[int] = 24


def move_odds(age_days: float) -> float:
    """Probability this price changes in the coming week, from its age alone.

    A price 22-28 days old moves the following week 96% of the time. One 7 days
    old moves 0.2% of the time. That gap is the entire basis for timing a
    reorder around the price rather than around the shelf.
    """
    for limit, odds in MOVE_ODDS:
        if age_days <= limit:
            return odds
    return MOVE_ODDS[-1][1]


def expected_price(price: float, age_days: float, rate_class: str) -> float:
    """What this price is worth in expectation next week.

    price x (1 +/- median move x probability of moving)

    The SIGN comes from the drug's rate class, and it is not cosmetic:

      G (generic)  -> DOWN. Generic prices erode. Each additional ANDA entrant
                      pushes the average acquisition cost lower, and NADAC is a
                      survey of what pharmacies actually paid.
      B / B-ANDA / B-BIO (brand) -> UP. CMS moves brand NADAC roughly 1:1 with
                      the manufacturer's published WAC increase.

    This is an EXPECTATION over the whole distribution, not a forecast of one
    drug's next price. It is the right input to a purchasing decision (which is
    itself an expectation over many purchases) and the wrong thing to quote to
    a patient.
    """
    direction = -1.0 if rate_class == "G" else 1.0
    return price * (1 + direction * MEDIAN_MOVE * move_odds(age_days))


def days_to_reprice(age_days: float) -> float:
    """Days until this price is most likely to change. 0 means it is due now."""
    return max(0.0, CYCLE_PEAK - age_days)


# how long the wholesaler takes. Most community pharmacies get next-day
# delivery on a daily order cycle; two days is the safe planning assumption.
LEAD_DAYS: Final[int] = 2


def reorder_advice(on_hand: float, monthly: float, age_days: float,
                   price: float, rate_class: str,
                   lead_days: int = LEAD_DAYS) -> dict:
    """When to reorder this drug, accounting for BOTH the shelf and the price.

    A pharmacy normally reorders on stock level alone: when you get low, you
    buy. That ignores the fact that NADAC reprices on a monthly cycle and you
    often have slack to move the order a few days either way.

    The two clocks:

      SHELF CLOCK   days_of_supply = on_hand / (monthly / 30)
                    order_by       = days_of_supply - lead_days
                    Past order_by you stock out. This clock is non-negotiable.

      PRICE CLOCK   days_to_reprice, from the drug's position in the monthly
                    cycle. Generic prices erode downward, brand prices step up
                    with WAC.

    The decision is whether the price clock fits inside the slack the shelf
    clock allows:

      * order_by has passed            -> ORDER NOW. Stock beats price, always.
      * price about to FALL (generic)
        and reprice lands before order_by -> WAIT. Buying after the drop costs
                                             less for the same units.
      * price about to RISE (brand)
        and reprice lands before order_by -> ORDER NOW, ahead of the increase.
      * otherwise                      -> ROUTINE. Order at order_by.

    Order quantity is one month of demand, which matches the monthly repricing
    rhythm -- buying deeper means holding inventory across a price change you
    cannot predict beyond the next cycle.

    ponytail: fixed one-month order quantity and a flat lead time. The real
    version is an EOQ against carrying cost and the wholesaler's break points;
    that needs cost-of-capital and bracket pricing, neither of which is in
    NADAC.
    """
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

    if order_by <= 0:
        action, why, act_on_price = "order now", "at or past stock-out", False
    elif likely and swing < 0 and to_reprice < order_by:
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
    elif likely and swing > 0 and to_reprice < order_by:
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
    """Script size at which the dispensing fee stops covering your cost gap.

    Reimbursement is NADAC*qty + fee; your outlay is cost*qty. So you go
    underwater once (cost - NADAC) * qty exceeds the fee -- which makes
    break-even QUANTITY the honest number, needing no dispensing data.
    Shared by `margin` and `forecast` so the "ANY qty" edge case lives once.
    """
    gap = cost - nadac
    if gap <= 0:
        return "never"
    qty = fee / gap
    # under one unit means the gap alone eats the whole fee: every script loses
    # money whatever its size. "0 ML" reads as a rounding bug, so say what it is.
    return f"{qty:,.0f} {unit}" if qty >= 1 else "ANY qty"


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
        rows = conn.execute(
            "SELECT ndc, description, CAST(unit_price AS REAL), pricing_unit"
            " FROM nadac WHERE as_of_date = ?"
            " AND ndc IN (%s)" % ",".join("?" * len(mine)),
            [newest_week(conn), *mine]).fetchall()
    finally:
        conn.close()

    fee = config.dispensing_fee

    def monthly_exposure(ndc, nadac):
        """Dollars this drug costs you per month, or None if volume is unknown.

        This is the number a pharmacist actually feels. Ranking by per-unit gap
        alone is misleading: a $76/unit gap on a drug dispensed twice a year
        matters less than $0.05 on one filled 200 times a month.
        """
        _, mo = qty.get(ndc, (None, None))
        return None if mo is None else (mine[ndc] - nadac) * mo

    have_vol = any(monthly_exposure(r[0], r[2]) is not None for r in rows)
    # rank by dollars where volume is known, per-unit gap where it is not
    rows = sorted(rows, reverse=True,
                  key=lambda r: (monthly_exposure(r[0], r[2]) if have_vol
                                 else mine[r[0]] - r[2]) or 0.0)

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
    from pharmacli.store import connect, costs, newest_week, stock

    config = load()
    conn = connect(args.db or config.db)
    try:
        mine, qty = costs(conn), stock(conn)
        if not mine:
            log.error("no inventory recorded. "
                      "pharmacli watch import data/pharmacy_inventory.csv")
            return EXIT_NOROWS
        week = args.as_of or newest_week(conn)
        if not conn.execute("SELECT 1 FROM nadac WHERE as_of_date=? LIMIT 1",
                            (week,)).fetchone():
            have = [w for (w,) in conn.execute(
                "SELECT DISTINCT as_of_date FROM nadac ORDER BY as_of_date DESC LIMIT 6")]
            log.error("no NADAC file for %s. recent: %s", week, ", ".join(have))
            return EXIT_BADIN
        rows = conn.execute(
            "SELECT ndc, description, CAST(unit_price AS REAL), pricing_unit,"
            " rate_class, CAST(julianday(as_of_date)-julianday(effective_date) AS INT)"
            " FROM nadac WHERE as_of_date = ? AND ndc IN (%s)"
            % ",".join("?" * len(mine)), [week, *mine]).fetchall()
    finally:
        conn.close()

    plan = []
    for ndc, desc, nadac, unit, rc, age in rows:
        oh, mo = qty.get(ndc, (None, None))
        if oh is None or mo is None:
            continue                       # no quantities: nothing to plan with
        a = reorder_advice(oh, mo, age, mine[ndc], rc, args.lead_days)
        if a["order_by"] is not None and a["order_by"] <= args.within:
            plan.append((a, ndc, desc, unit, age))

    if not plan:
        print(f"nothing needs ordering inside {args.within} days.")
        return EXIT_OK

    plan.sort(key=lambda x: x[0]["order_by"])
    print(f"\nREORDER  (NADAC file {week}, {args.lead_days}-day lead time)")
    print(f"{'drug':<31} {'on hand':>9} {'order':>10} {'qty':>7} {'saves':>8}  why")
    print("-" * 104)
    saved = 0.0
    limit = args.top or len(plan)
    for i, (a, ndc, desc, unit, age) in enumerate(plan):
        saved += a["saving"]                     # total spans every line
        if i >= limit:
            continue
        sv = f"${a['saving']:,.2f}" if a["saving"] else "-"
        print(f"{str(desc)[:30]:<31} {a['days_left']:>6.0f}d   {a['action']:>10} "
              f"{a['qty']:>7,} {sv:>8}  {a['why']}")
    if limit < len(plan):
        print(f"... {len(plan) - limit:,} more due in this window (--top 0 for all)")
    print(f"\n{len(plan)} of {len(rows)} stocked drugs need an order inside "
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


# The buying desk: one self-contained HTML page built from the panel and from
# the inventory you imported. Not a mockup with baked-in numbers -- every drug,
# price, cost and quantity is read out of the database at build time, so the
# page is only ever as real as `watch import` made it.
def cmddesk(args: argparse.Namespace) -> int:
    import json
    import re
    from collections import defaultdict
    from datetime import date as _date
    from importlib.resources import files

    from pharmacli.store import connect, costs, newest_week, stock

    config = load()
    db = args.db or config.db
    conn = connect(db)
    try:
        week = args.as_of or newest_week(conn)
        if week is None:
            log.error("no NADAC data. pharmacli fetch")
            return EXIT_NOROWS
        rows = conn.execute(
            "SELECT ndc, description, CAST(unit_price AS REAL), pricing_unit,"
            " rate_class, CAST(julianday(as_of_date)-julianday(effective_date) AS INT),"
            " CAST(NULLIF(generic_price,'') AS REAL)"
            " FROM nadac WHERE as_of_date = ? AND unit_price != ''", (week,)).fetchall()
        if not rows:
            have = [w for (w,) in conn.execute(
                "SELECT DISTINCT as_of_date FROM nadac ORDER BY as_of_date DESC LIMIT 6")]
            log.error("no NADAC file for %s. recent: %s", week, ", ".join(have))
            return EXIT_BADIN
        mine, qty = costs(conn), stock(conn)
    finally:
        conn.close()

    # molecule = the description with strength and form stripped, so every
    # labeller's version of one drug groups under a single heading in search
    def mol(desc: str) -> str:
        return re.split(r"\s+\d|\s+(?:TAB|CAP|SOLN|SUSP|CREAM|OINT|GEL|INJ|ER |DR )",
                        desc, 1)[0].strip()

    fam: dict[str, list] = defaultdict(list)
    for r in rows:
        fam[mol(r[1])].append(r)

    # Selection, in priority order. The whole formulary is 30k NDCs a week --
    # too much to inline -- so this takes a sample that is useful rather than
    # random: everything you stock, then breadth, then depth on the drugs that
    # actually have many labellers.
    picked: dict[str, tuple] = {}
    for r in rows:                                   # 1. every NDC you stock
        if r[0] in mine:
            picked[r[0]] = r
    # --limit bounds the SAMPLE, and your shelf is exempt from it: pass 1 already
    # ran, so the cap can only ever be at least that big. Without this the
    # breadth pass overshot -- 5,000 stocked plus 2 per drug name gave 8,114 NDCs
    # from a --limit of 5,000.
    cap = max(args.limit, len(picked))
    for v in fam.values():                           # 2. breadth: 2 per drug name,
        if len(picked) >= cap:
            break
        brand = [r for r in v if r[4] != "G" and r[6]]   # preferring a brand that
        gen = [r for r in v if r[4] == "G"]              # carries a generic price
        for r in (brand[:1] + gen[:1] + v)[:2]:
            if len(picked) >= cap:
                break
            picked.setdefault(r[0], r)
    for v in sorted(fam.values(), key=len, reverse=True):   # 3. depth, until full
        if len(picked) >= cap:
            break
        for r in v:
            if len(picked) >= cap:
                break
            picked.setdefault(r[0], r)

    RC = {"G": 0, "B": 1, "B-ANDA": 2, "B-BIO": 3}
    drugs = []
    for ndc, desc, price, unit, rc, age, gp in sorted(picked.values(), key=lambda r: r[1]):
        oh, mo = qty.get(ndc, (None, None))
        drugs.append([ndc, desc, round(price, 5), unit, RC.get(rc, 1), age,
                      round(gp, 5) if gp else 0,
                      oh or 0, mo or 0, round(mine[ndc], 5) if ndc in mine else 0, 1])

    payload = {"week": week, "weekTotal": len(rows), "drugs": drugs,
               "db": db, "built": _date.today().isoformat(),
               "pharmacy": args.pharmacy}
    page = (files("pharmacli") / "desk.html").read_text(encoding="utf-8")
    out = Path(args.outfile or "desk.html")
    try:
        out.write_text(page.replace("/*DATA*/", json.dumps(payload, separators=(",", ":"))),
                       encoding="utf-8")
    except OSError as exc:
        log.error("cannot write %s: %s", out, exc)
        return EXIT_BADIN

    shelf = sum(1 for d in drugs if d[7] and d[8])
    print(f"{out}  ({out.stat().st_size // 1024} KB)")
    print(f"  {len(drugs):,} NDCs from the {week} file (of {len(rows):,} that week)")
    print(f"  {shelf} priced shelf positions from your inventory import")
    if not shelf:
        log.warning("no stocked drugs have both on_hand and monthly -- "
                    "the reorder worklist will be empty. "
                    "pharmacli watch import data/pharmacy_inventory.csv")
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
    "desk":     cmddesk,
}


def dispatch(args: argparse.Namespace) -> int:
    if args.command in HANDLERS:
        return HANDLERS[args.command](args)

    # everything below here reads the input CSV: clean, dedup, sort
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


