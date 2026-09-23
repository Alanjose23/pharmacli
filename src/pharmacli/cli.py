import argparse
import asyncio
import logging
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Final

# import reader, rules, models, errors
from pharmacli.config import load
from pharmacli.desk import BLOCKS, _sparkline, breakeven, format_worklists, worklists
from pharmacli.err import FetchError, PharmaError, RuleError
from pharmacli.models.rows import COLUMNS
from pharmacli.output import open_output
from pharmacli.parser import buildparse
from pharmacli.protocols import BaseRule
from pharmacli.reader import read_csv
from pharmacli.rules import describe, make

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

        log.info("fetching %d pages, %d requests in flight", len(pages), workers)

        # each page is written after it comes back, not after whole program
        stored = 0

        def on_page(records):
            nonlocal stored
            stored += insert(conn, records)

        start = time.perf_counter()
        try:
            # the sequential/threaded/multiproc models exist for `bench`
            records = asyncio.run(collect.fetch_async(pages, workers, on_page))
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
                skipped.append((line, "needs at least ndc,cost"))
                continue
            ndc = raw[0].strip()
            if not ndc:
                skipped.append((line, "missing NDC"))
                continue
            if not ndc.isdigit():
                # the header row lands here, which is the common case and why
                # this is a skip rather than an error
                skipped.append((line, f"NDC {ndc!r} is not digits"))
                continue

            cost, err = num(raw[1], "cost")
            if err or cost is None:
                skipped.append((line, err or "missing cost"))
                continue
            oh, err = num(raw[2], "on_hand") if len(raw) > 2 else (None, None)
            if err:
                skipped.append((line, err))
                continue
            mo, err = num(raw[3], "monthly") if len(raw) > 3 else (None, None)
            if err:
                skipped.append((line, err))
                continue
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
    from pharmacli.store import connect, last_priced, newest_week, watch_add, watch_remove, watched

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
        print(f"\n  {'variant':<8}{'bal acc':>9}{'F1':>8}"
              f"{'precision':>11}{'recall':>8}{'features':>10}")
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
        from pharmacli.frame import load as load_frame  # pandas stays inside the
    except ImportError:                                  # handler: a bare install
        log.error('forecast needs: pip install -e ".[analysis]"')   # must import
        return EXIT_BADIN                                            # cleanly

    from pharmacli.store import connect, watched
    from pharmacli.store import costs as watch_costs

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
        holes = ",".join("?" * len(mine))
        rows = conn.execute(
            "SELECT ndc, description, CAST(unit_price AS REAL), pricing_unit"
            " FROM nadac WHERE as_of_date = ?"
            f" AND ndc IN ({holes})",
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
    for i, (a, ndc, desc, _unit, _age) in enumerate(plan):
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
    import socket
    import threading
    import webbrowser

    import uvicorn

    from pharmacli.serve import app, issue

    if args.db:
        os.environ["PHARMACLI_DB"] = args.db      # the routes read it from config
    # the token rides in the fragment, so it reaches the page without ever being
    # sent to the server or landing in a log
    url = f"http://127.0.0.1:{args.port}/chat#token={issue('chat')}"
    print(f"pharmacli chat on {url}   (ctrl-c to stop)")

    def open_when_up() -> None:
        # uvicorn has not bound the port yet, and the first boot builds the
        # panel, so opening the browser now would show a refused connection
        for _ in range(600):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", args.port)) == 0:
                    webbrowser.open(url)
                    return
            time.sleep(0.1)

    if not args.no_open:
        threading.Thread(target=open_when_up, daemon=True).start()
    # 127.0.0.1, never 0.0.0.0: it answers with your acquisition costs
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_config=None)
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
           ("ndc", "phrase"))
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
    run(args.workload, args.limit, args.workers or config.workers, bench_db)
    return EXIT_OK


def _docs_fetch(args: argparse.Namespace) -> int:
    import httpx

    from pharmacli import docs

    config = load()
    with httpx.Client(timeout=config.timeout) as client:
        if args.from_shelf:
            corpus = docs.from_shelf(args.db or config.db, args.from_shelf, client)
            if not corpus["inserts"]:
                log.error("no shelf drug resolved to a label -- "
                          "pharmacli watch import data/pharmacy_inventory.csv")
                return EXIT_NOROWS
            docs.save_corpus(corpus)
            print(f"{len(corpus['inserts'])} labels, {len(corpus['business'])} "
                  f"reimbursement documents -> {docs.CORPUS}")
            return EXIT_OK
        corpus = docs.load_corpus()
        failed = docs.fetch(corpus, client)
    total = len(corpus["inserts"]) + len(corpus["business"])
    print(f"{total - len(failed)} of {total} documents in {docs.DOCS_DIR}")
    for doc_id in failed:
        log.warning("not fetched: %s", doc_id)
    return EXIT_OK if len(failed) < total else EXIT_NETWORK


def _docs_ingest(args: argparse.Namespace) -> int:
    import httpx

    from pharmacli import docs, rag

    changed: list[str] = []
    before = ""
    if args.action == "refresh":
        corpus = docs.load_corpus()
        # taken before refresh mutates corpus in place, so reindex can tell
        # whether the index on disk was actually complete for THIS corpus
        before = docs.digest(corpus)
        with httpx.Client(timeout=load().timeout) as client:
            changed = docs.refresh(corpus, client)
        if not changed:
            # do not return here: a stack-down ingest earlier could have left
            # the index partial or stale even though nothing changed just now
            print("every document is current")
        else:
            from pharmacli.safety import label_digest

            docs.save_corpus(corpus)
            print(f"{len(changed)} changed: {', '.join(changed)}")
            titles = {e["doc_id"]: e["title"] for e in corpus["inserts"] + corpus["business"]}
            for doc_id in changed:
                prev = docs.pdf_path(doc_id).with_suffix(".prev.pdf")
                if prev.exists():
                    print(f"\nlabel changes -- {titles[doc_id]}")
                    print("\n".join(label_digest(prev, docs.pdf_path(doc_id))))

    async def go() -> dict:
        async with rag.session() as r:
            if args.action == "refresh":
                return await rag.reindex(r, changed, before)
            return await rag.ingest(r)

    report = asyncio.run(go())
    if report["skipped"]:
        print("corpus already indexed")
        return EXIT_OK
    print(f"{report['docs']} documents indexed: "
          + ", ".join(f"{n} {k} chunks" for k, n in report["chunks"].items()))
    if report["missing"]:
        log.warning("%d documents not on disk (pharmacli docs fetch): %s",
                    len(report["missing"]), ", ".join(report["missing"]))
    return EXIT_OK if report["docs"] else EXIT_NOROWS


def _docs_eval(args: argparse.Namespace) -> int:
    import json

    from pharmacli import rag, rageval

    if args.compare:
        try:
            a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in args.compare)
        except (OSError, ValueError) as exc:
            log.error("cannot read %s: %s", args.compare, exc)
            return EXIT_BADIN
        print("\n".join(rageval.compare(a, b)))
        return EXIT_OK
    config = load()
    try:
        rows = rageval.load_rows(args.qa)
    except (OSError, ValueError) as exc:
        log.error("cannot read %s: %s", args.qa, exc)
        return EXIT_BADIN

    async def go() -> dict:
        async with rag.session() as r:
            return await rageval.run(r, rows, args.chunking or config.chunking,
                                     args.k or config.k, args.retrieval_only)

    result = asyncio.run(go())
    print(f"\n{len(rows)} questions, {result['config']['chunking']} chunks, "
          f"k={result['config']['k']}")
    for key, value in result["summary"].items():
        print(f"  {key:<20} {value}")
    _dump(args, result)
    return EXIT_OK


def _docs_appeal(args: argparse.Namespace) -> int:

    from pharmacli import rag

    if len(args.target) != 1:
        log.error("usage: pharmacli docs appeal NDC [-o appeal.md]")
        return EXIT_BADIN
    facts = rag.appeal_facts(args.db or load().db, args.target[0])
    if isinstance(facts, str):
        log.error("%s", facts)
        return EXIT_NOROWS

    async def go() -> dict:
        async with rag.session() as r:
            return await rag.draft_appeal(r, facts)

    got = asyncio.run(go())
    if args.out:
        Path(args.out).write_text(got["markdown"], encoding="utf-8")
        print(f"draft -> {args.out}")
    else:
        print(got["markdown"])
    if got["flagged"]:
        log.warning("numbers in the draft that no source supports: %s", ", ".join(got["flagged"]))
    return EXIT_OK


def _docs_handout(args: argparse.Namespace) -> int:
    from pharmacli import docs, safety

    if not args.target:
        log.error("usage: pharmacli docs handout DRUG [-o handout.md]")
        return EXIT_BADIN
    try:
        md = safety.handout(" ".join(args.target), docs.load_corpus())
    except LookupError as exc:
        log.error("%s", exc)
        return EXIT_NOROWS
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"handout -> {args.out}")
    else:
        print(md, end="")
    return EXIT_OK


DOCS = {"fetch": _docs_fetch, "ingest": _docs_ingest, "refresh": _docs_ingest,
        "eval": _docs_eval, "appeal": _docs_appeal, "handout": _docs_handout}


def cmddocs(args: argparse.Namespace) -> int:
    import httpx

    from pharmacli import docs

    # every action reads corpus.json, and all but fetch and handout need the two
    # services. Both failures read the same whichever action hit them.
    try:
        return DOCS[args.action](args)
    except (OSError, ValueError) as exc:
        log.error("cannot read %s (or a label PDF): %s", docs.CORPUS, exc)
        return EXIT_BADIN
    except httpx.HTTPError as exc:
        log.error("%s needs Chroma and Ollama -- docker compose up -d chroma ollama (%s)",
                  args.action, exc)
        return EXIT_NETWORK


def cmdclaims(args: argparse.Namespace) -> int:
    from pharmacli import claims

    db = args.db or load().db
    if args.action == "sample":
        out, payers = args.out or "data/claims_sample.csv", args.payers or "data/payers.csv"
        n = claims.sample(db, out, payers, args.days, args.seed)
        if not n:
            log.error("no shelf drug has monthly volume and a NADAC price to sample from")
            return EXIT_NOROWS
        print(f"{n:,} synthetic claims -> {out}" + (" (capped)" if n == claims.MAX_SAMPLE else ""))
        print(f"illustrative payer fees -> {payers} -- replace them with your contract fees")
        return EXIT_OK

    if not args.file:
        log.error("usage: pharmacli claims check CLAIMS.csv [--payers PAYERS.csv]")
        return EXIT_BADIN
    try:
        rows, skipped = claims.read_claims(args.file)
        fees = claims.read_payers(args.payers)
    except (OSError, ValueError, KeyError) as exc:
        log.error("cannot read claims or payers: %s", exc)
        return EXIT_BADIN
    for line, why in skipped[:MAX_SKIPPED]:
        log.info("line %d: %s", line, why)
    if not rows:
        log.error("no usable claim rows in %s", args.file)
        return EXIT_NOROWS
    result = claims.check(db, rows, fees)
    print("\n".join(claims.report(result, len(skipped), args.top)))
    if args.out:
        print(f"{claims.write_underpaid(result, args.out):,} underpaid claims -> {args.out}")
    return EXIT_OK


def cmdgenerics(args: argparse.Namespace) -> int:
    from pharmacli import claims

    got = claims.generics(args.db or load().db)
    if isinstance(got, str):
        log.error("%s", got)
        return EXIT_NOROWS
    print("\n".join(claims.generic_lines(got, args.top)))
    return EXIT_OK


def cmdrecalls(args: argparse.Namespace) -> int:
    import httpx

    from pharmacli import safety

    try:
        print("\n".join(safety.shelf_recalls(args.db, args.days)))
    except httpx.HTTPError as exc:
        log.error("openFDA recall search failed: %s", exc)
        return EXIT_NETWORK
    return EXIT_OK


def cmdinteractions(args: argparse.Namespace) -> int:
    import httpx

    from pharmacli import docs, rag, safety

    if not 2 <= len(args.drugs) <= safety.MAX_DRUGS:
        log.error("give 2 to %d drugs (15 pairs is ~10 minutes on this CPU)", safety.MAX_DRUGS)
        return EXIT_BADIN

    async def go() -> None:
        async with rag.session() as r:
            async for line in safety.interactions(r, args.drugs):
                print(line, flush=True)            # a pair at a time, not all at the end

    try:
        asyncio.run(go())
    except (OSError, ValueError) as exc:
        log.error("cannot read %s: %s", docs.CORPUS, exc)
        return EXIT_BADIN
    except (httpx.HTTPError, ConnectionError) as exc:
        log.error("interactions need Chroma and Ollama -- "
                  "docker compose up -d chroma ollama (%s)", exc)
        return EXIT_NETWORK
    return EXIT_OK


# commands that do NOT read an input CSV. Was nine near-identical
# `if args.command == "x": return cmdx(args)` blocks; a dict says the same thing
# and a new command is now one line here instead of three.
# needs the same PHARMACLI_JWT_SECRET the API runs with, or the token it prints
# will not verify -- see serve.py
def cmdtoken(args: argparse.Namespace) -> int:
    from pharmacli.serve import issue

    print(issue(args.sub, args.ttl))
    return EXIT_OK


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
    "docs":     cmddocs,
    "claims":   cmdclaims,
    "generics": cmdgenerics,
    "recalls":  cmdrecalls,
    "interactions": cmdinteractions,
    "token":    cmdtoken,
}


def dispatch(args: argparse.Namespace) -> int:
    if args.command in HANDLERS:
        return HANDLERS[args.command](args)

    # the only command left reads the input CSV: clean
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

    kept = tuple(report.records)
    for rule in rules:
        kept, removed = rule.apply(kept)
        log.info("%s: rows removed: %d", rule.name, removed)

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
    except ModuleNotFoundError as exc:
        # an optional extra is missing; say which package instead of a traceback.
        # A missing pharmacli module is a real bug, so that one still raises
        if not exc.name or exc.name.startswith("pharmacli"):
            raise
        log.error("%s is not installed -- see the extras table in README.md", exc.name)
        return EXIT_BADIN
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_OK



if __name__ == "__main__":
    raise SystemExit(main())


