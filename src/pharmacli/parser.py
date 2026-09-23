# Every command's arguments, and nothing else. Split out of cli.py, which held
# the parser, the price maths and every handler in one 1,822-line file.
#
# Imports run one way: cli -> parser -> desk. Nothing here imports cli.

import argparse
from importlib.metadata import version
from pathlib import Path

from pharmacli.desk import LEAD_DAYS
from pharmacli.rules import RULES


# build command line parser
def buildparse() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="PharmaCLI",
        description="CLI tool for parsing inventory CSVs",
        epilog="use -n/ --dryrun to see what would change without writing")
    # from the installed package, not a second copy that drifts from pyproject
    parser.add_argument("--version", action="version",
                        version=f"PharmaCLI {version('pharmacli')}")

    # define output input and dryrun tags


    # supressing so newer iterations never stop the previous ones
    verbosity = argparse.ArgumentParser(add_help=False)
    verbosity.add_argument("-v", "--verbose", action="store_true",
                           default=argparse.SUPPRESS, help="debug logging")

    fileopts = argparse.ArgumentParser(add_help=False)
    fileopts.add_argument("input", type = Path, help = "path to input CSV")
    fileopts.add_argument("-o", "--outfile", type=Path, default=None,
                          help="write to here instead of stdout")
    fileopts.add_argument("-n", "--dryrun", action="store_true",
                          help="report only, write nothing")



    commands = parser.add_subparsers(dest="command", required= True, metavar="COMMAND")
    # add new rules command
    clean = commands.add_parser("clean", parents=[fileopts, verbosity],
                                help="run one or more rules")
    clean.add_argument("--rule", action="append", default=[], metavar="NAME[:KEY=VALUE, ...]",
                       help=f"repeatable; available: {', '.join(sorted(RULES))}")



    # list rules and columns
    commands.add_parser("rules", parents=[verbosity], help="list the CSV cleaning rules")
    commands.add_parser("columns", parents=[verbosity], help="list the expected columns")



    # net commands
    fetch = commands.add_parser("fetch", parents=[verbosity],help="pull NADAC records into SQLite")
    fetch.add_argument("--workers", type=int, default=None,
                       help="pool size / max requests in flight")
    fetch.add_argument("--limit", type=int, default=5000,help="how many records to fetch")
    fetch.add_argument("--db", default=None, help="SQLite file to write to")
    fetch.add_argument("--fresh", action="store_true",
                       help="delete the database first, so the fetch is timed from empty "
                            "instead of resuming")
    # the API takes up to 5000 rows a request. the 100 default stays because bench
    # needs many small pages to have any concurrency to measure -- a bulk load wants
    # the opposite, so it is a flag rather than a new constant
    fetch.add_argument("--page-size", type=int, default=None, metavar="N",
                       help="records per request, API max 5000 (default 100)")

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

    # week 9: the documents behind `ask`'s documents lane. Shaped like `watch`:
    # one command, an action, and the flags each action reads.
    dcs = commands.add_parser("docs", parents=[verbosity],
                              help="the RAG corpus: fetch, ingest, refresh, eval, appeal, handout")
    # late, so cli can import this module: by the time buildparse runs, cli is
    # fully imported. One table, no mirrored copy to keep in step.
    from pharmacli.cli import DOCS
    dcs.add_argument("action", choices=sorted(DOCS))
    dcs.add_argument("target", nargs="*", help="appeal: the NDC; handout: the drug")
    # run once by the developer; the corpus.json it writes is committed, so a
    # clean checkout needs neither the DVC database nor the inventory
    dcs.add_argument("--from-shelf", type=int, default=None, metavar="N",
                     help="fetch: rebuild corpus.json from the N highest-spend shelf drugs")
    dcs.add_argument("--qa", default="tests/rag_qa.jsonl", help="eval: the answer key")
    dcs.add_argument("--chunking", choices=("fixed", "section"), default=None)
    dcs.add_argument("--k", type=int, default=None, help="passages retrieved per question")
    dcs.add_argument("--retrieval-only", action="store_true",
                     help="eval: score retrieval without generating (seconds, not minutes)")
    dcs.add_argument("--compare", nargs=2, default=None, metavar="RUN.json",
                     help="eval: the before/after table of two --json runs")
    dcs.add_argument("--json", default=None, metavar="FILE",
                     help="eval: also write the full result here")
    dcs.add_argument("-o", "--out", default=None, metavar="FILE",
                     help="appeal, handout: write here instead of stdout")
    dcs.add_argument("--db", default=None)

    # reimbursement: synthetic claims until a real export exists, then the check
    clm = commands.add_parser("claims", parents=[verbosity],
                              help="claims paid below NADAC + fee, or below your cost")
    clm.add_argument("action", choices=("sample", "check"))
    clm.add_argument("file", nargs="?", default=None, help="check: the claims CSV")
    clm.add_argument("--payers", default=None, metavar="CSV",
                     help="payer,dispensing_fee (sample writes data/payers.csv)")
    clm.add_argument("-o", "--out", default=None, metavar="FILE",
                     help="sample: the claims CSV (data/claims_sample.csv); "
                          "check: underpaid claims CSV")
    clm.add_argument("--days", type=int, default=30, help="sample: spread fills over N days")
    clm.add_argument("--seed", type=int, default=7, help="sample: same seed, same file")
    clm.add_argument("--top", type=int, default=25, metavar="N")
    clm.add_argument("--db", default=None)

    gen = commands.add_parser("generics", parents=[verbosity],
                              help="brand stock with a cheaper NADAC generic, in $/month")
    gen.add_argument("--top", type=int, default=25, metavar="N")
    gen.add_argument("--db", default=None)

    rec = commands.add_parser("recalls", parents=[verbosity],
                              help="FDA drug recalls that touch your shelf")
    rec.add_argument("--days", type=int, default=90)
    rec.add_argument("--db", default=None)

    inter = commands.add_parser("interactions", parents=[verbosity],
                                help="what the labels say about 2-6 drugs taken together")
    inter.add_argument("drugs", nargs="+", help="drug names or NDCs")

    bench = commands.add_parser("bench", parents=[verbosity],help="time the concurrency models")
    bench.add_argument("--workload", default="both", choices=("io", "cpu", "numpy", "both"),
                       help="io = network fetch, cpu = parsing, numpy = vectorization, "
                            "both = run each")
    bench.add_argument("--workers", type=int, default=None)
    bench.add_argument("--limit", type=int, default=2000)
    bench.add_argument("--db", default=None)

    tok = commands.add_parser("token", parents=[verbosity],
                              help="mint a bearer token for the API")
    tok.add_argument("--sub", default="cli", help="who the token is for; it lands in the logs")
    tok.add_argument("--ttl", type=int, default=3600, help="seconds until it expires")

    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    return parser
