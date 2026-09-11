import collections
import difflib
import functools
import io
import json
import logging
import math
import pathlib
import re
import sys
import tokenize
from collections.abc import Callable

from pharmacli.config import load

log = logging.getLogger(__name__)

# identifiers and words, lowercased. Catches MATERIAL_SWING and age_days whole,
# which is the point -- those rare exact tokens are what the "why" questions
# actually turn on.
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


def _words(s: str) -> list[str]:
    return _WORD.findall(s.lower())


# Filler that every label shares. Without stripping these, "what dose of
# lisinopril should I give" scores against "what is on my shelf" on the word
# "what" alone and gets ANSWERED -- a clinical question served a shelf total.
# Matching has to turn on content words only.
_STOP = frozenset("""
what is the a an on my of do does this that i am are should would can it to for
in and me you how when which be by at with was were will not no yes get give
""".split())


def _key(s: str) -> set:
    return set(_words(s)) - _STOP


# ── the money lane ───────────────────────────────────────────────────────────
# Each of these returns LINES, so the caller only has to print. The three
# worklist questions slice cli.format_worklists rather than re-wording it, so
# `ask` and `today` cannot drift.

def _worklists(db, within=14, dead_days=90):
    from pharmacli.cli import worklists
    return worklists(db or load().db, within, dead_days)


def _section(w, tag, within=14, dead_days=90) -> list[str]:
    from pharmacli.cli import format_worklists

    lines = format_worklists(w, within, dead_days)
    i = next(n for n, s in enumerate(lines) if s.startswith(tag))
    j = next((n for n in range(i + 1, len(lines)) if lines[n] == ""), len(lines))
    return lines[:4] + lines[i:j]


def underwater(db=None) -> list[str]:
    w = _worklists(db)
    return [w["error"]] if "error" in w else _section(w, "APPEAL")


# Fixed horizons. `today --within` and `reorder --within` already take the
# knob; a question interface the browser cannot pass flags to does not need it.
def buy_worklist(db=None) -> list[str]:
    w = _worklists(db)
    return [w["error"]] if "error" in w else _section(w, "BUY")


def dead_stock(db=None) -> list[str]:
    w = _worklists(db)
    return [w["error"]] if "error" in w else _section(w, "CLEAR")


def next_repricing(db=None) -> list[str]:
    from datetime import date

    from pharmacli.cli import next_reprice

    today = date.today()
    nxt = next_reprice(today)
    return [f"next repricing file {nxt} ({(nxt - today).days} days from today)",
            "the NADAC monthly lands on the first Monday on or after the 15th;",
            "the weekly file carrying it is dated that Wednesday."]


def shelf_summary(db=None) -> list[str]:
    from pharmacli.store import connect, costs, newest_week, stock

    conn = connect(db or load().db)
    try:
        week, mine, qty = newest_week(conn), costs(conn), stock(conn)
    finally:
        conn.close()
    vol = sum(1 for v in qty.values() if v[1])
    return [f"{len(mine):,} positions with a recorded cost",
            f"{vol:,} of them have a dispensing volume on file",
            f"newest NADAC file {week}"]


def _drug(db, ndc: str):
    from datetime import date as _d

    from pharmacli import enrich
    from pharmacli.store import connect

    d = enrich.local(ndc, db)
    if not d:
        return None
    key = ndc.strip().zfill(11)
    conn = connect(db or load().db)
    try:
        # one row by primary key -- was store.costs() + store.stock(), which read
        # the whole shelf to answer for one drug (~182s to write 5,000 drugs)
        row = conn.execute("SELECT cost, on_hand, monthly FROM watch WHERE ndc = ?",
                           (key,)).fetchone()
    finally:
        conn.close()
    cost, qty = (row[0], (row[1], row[2])) if row else (None, (None, None))
    age = (_d.fromisoformat(d["as_of_date"])
           - _d.fromisoformat(d["effective_date"])).days
    return d, cost, qty, age


NOT_FOUND = "{} is not in the panel. `pharmacli ask` -> find a drug by description"


def drug_info(db=None, ndc="") -> list[str]:
    got = _drug(db, ndc)
    if not got:
        return [NOT_FOUND.format(ndc)]
    d = got[0]
    return [f"{d['description']}",
            f"  NDC          {ndc}",
            f"  NADAC        ${d['unit_price']}/{d['pricing_unit']}",
            f"  class        {d['rate_class']}    OTC {d['otc']}",
            f"  effective    {d['effective_date']}   file {d['as_of_date']}"]


def price_history(db=None, ndc="") -> list[str]:
    from pharmacli import enrich

    h = enrich.history(ndc, db)
    if not h:
        return [NOT_FOUND.format(ndc)]
    first, last = h[0][1], h[-1][1]
    pct = (last - first) / first * 100 if first else 0.0
    out = [f"{len(h)} weekly files, {h[0][0]} to {h[-1][0]}",
           f"  ${first:.5f} -> ${last:.5f}   {pct:+.1f}%"]
    # only the weeks where it actually moved: on 25 of 33 files nothing does
    moves = [(d, p) for (d, p), (_, prev) in zip(h[1:], h) if p != prev]
    out.append(f"  {len(moves)} of {len(h) - 1} weeks carried a change")
    out += [f"    {d}  ${p:.5f}" for d, p in moves[-5:]]
    return out


def move_probability(db=None, ndc="") -> list[str]:
    from pharmacli.cli import days_to_reprice, expected_price, move_odds

    got = _drug(db, ndc)
    if not got:
        return [NOT_FOUND.format(ndc)]
    d, _, _, age = got
    price, rc = float(d["unit_price"]), d["rate_class"]
    return [f"{d['description']}",
            f"  price is {age} days old",
            f"  odds of a move next week   {move_odds(age):.1%}",
            f"  most likely to move in     {days_to_reprice(age):.0f} days",
            f"  expected price next week   ${expected_price(price, age, rc):.5f}"
            f"  (now ${price:.5f})",
            "  an EXPECTATION over the whole class, not a forecast for this drug."]


def reorder_plan(db=None, ndc="") -> list[str]:
    from pharmacli.cli import MATERIAL_SWING, reorder_advice

    got = _drug(db, ndc)
    if not got:
        return [NOT_FOUND.format(ndc)]
    d, cost, (oh, mo), age = got
    if oh is None or not mo:
        return [f"{d['description']}",
                "  no on_hand / monthly volume recorded for this NDC.",
                "  pharmacli watch import data/pharmacy_inventory.csv"]
    a = reorder_advice(oh, mo, age, cost if cost is not None else float(d["unit_price"]),
                       d["rate_class"], min_swing=MATERIAL_SWING)
    out = [f"{d['description']}", f"  {a['action']} -- {a['why']}"]
    if a["days_left"] is not None:
        out.append(f"  days of supply left  {a['days_left']:.0f}")
    out.append(f"  order {a['qty']:,} units, worth ${a['saving']:,.2f} in timing")
    return out


def breakeven_qty(db=None, ndc="") -> list[str]:
    from pharmacli.cli import breakeven

    got = _drug(db, ndc)
    if not got:
        return [NOT_FOUND.format(ndc)]
    d, cost, _, _ = got
    if cost is None:
        return [f"{d['description']}", "  no acquisition cost recorded for this NDC."]
    fee = load().dispensing_fee
    # breakeven returns a STRING ("ANY qty", "never"); relay it, never compute on it
    return [f"{d['description']}",
            f"  your cost ${cost:.5f} vs NADAC ${float(d['unit_price']):.5f}",
            f"  break-even at ${fee:.2f} dispensing fee:  "
            f"{breakeven(cost, float(d['unit_price']), fee, d['pricing_unit'])}"]


@functools.lru_cache(maxsize=1)
def _embedder():
    from sentence_transformers import SentenceTransformer

    from pharmacli.nlp import EMB_MODEL
    return SentenceTransformer(EMB_MODEL)


def find_drug(db=None, phrase="") -> list[str]:
    from pharmacli.store import connect

    conn = connect(db or load().db)
    try:
        # description -> its most recent NDC. nlp.corpus() is DISTINCT on
        # description and returns no NDC, so a phrase would resolve to a name
        # with nothing to look up.
        rows = conn.execute(
            "SELECT description, ndc, MAX(as_of_date) FROM nadac"
            " WHERE description != '' GROUP BY description").fetchall()
    finally:
        conn.close()
    texts = [r[0] for r in rows]
    ndcs = {r[0]: r[1] for r in rows}
    try:
        from pharmacli.nlp import embed, semantic_search
        hits = semantic_search(texts, embed(texts), _embedder(), phrase)
        out = [f"semantic matches for {phrase!r}:"]
        out += [f"  {s:.3f}  {t}   {ndcs[t]}" for t, s in hits]
    except Exception:                       # no [nlp] extra, or no cached vectors
        from pharmacli.nlp import keyword_search
        names = keyword_search(texts, phrase)
        out = [f"keyword matches for {phrase!r}"
               " (install .[nlp] for semantic search):"]
        out += [f"  {t}   {ndcs[t]}" for t in names] or ["  nothing matched"]
    out.append("  scores are similarity, NOT confidence that this is your drug.")
    return out


def model_report(db=None) -> list[str]:
    p = pathlib.Path(load().model_path).with_suffix(".json")
    if not p.exists():
        return ["no model promoted yet -- run `pharmacli sweep`"]
    try:
        c = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("unreadable model sidecar %s: %s", p, exc)
        return [f"unreadable model sidecar {p}"]
    def r(x, n=4):                       # 17 digits of PR-AUC is not an answer
        return f"{x:.{n}f}" if isinstance(x, (int, float)) else x

    lift = ""
    if isinstance(c.get("test_pr_auc"), float) and c.get("baseline_pr_auc"):
        lift = f"   ({c['test_pr_auc'] / c['baseline_pr_auc']:.1f}x the bar)"
    out = [f"{c.get('name')} v{c.get('version')}   trained {c.get('trained_at')}",
           f"  test PR-AUC   {r(c.get('test_pr_auc'))}{lift}",
           f"  validation    {r(c.get('val_pr_auc'))}",
           f"  {c.get('baseline_name', 'baseline')}  {r(c.get('baseline_pr_auc'))}",
           f"  acts above p = {r(c.get('threshold'), 3)}",
           f"  panel {c.get('data_md5')}",
           "",
           "it REFUSES anything outside these measured ranges:"]
    for k, (lo, hi) in (c.get("bounds") or {}).items():
        out.append(f"    {k:<16} {r(lo, 2)} .. {r(hi, 2)}")
    for k, v in (c.get("categories") or {}).items():
        out.append(f"    {k:<16} {', '.join(map(str, v))}")
    return out


# ── the rationale lane ───────────────────────────────────────────────────────

def _comment_blocks(src: str) -> list[tuple[int, int, str]]:
    blocks: list[tuple[int, int, str]] = []
    run: list[str] = []
    start, prev = 0, -2
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type != tokenize.COMMENT:
            continue
        line = tok.start[0]
        if line != prev + 1:                        # a gap ends the block
            if run:
                blocks.append((start, prev, "\n".join(run)))
            run, start = [], line
        run.append(tok.string.lstrip("#").strip())
        prev = line
    if run:
        blocks.append((start, prev, "\n".join(run)))
    return blocks


# a comment block documents the thing UNDER it, and that name is usually absent
# from the prose: the MATERIAL_SWING block never types "MATERIAL_SWING", it just
# explains it. Without this, asking for a constant by name finds the code that
# REFERENCES it rather than the measurement that sets it.
_DEFINES = re.compile(r"^(?:def|class)\s+(\w+)|^([A-Z_][A-Z0-9_]*)\s*[:=]")


def _defined_below(lines: list[str], end: int) -> str:
    for probe in lines[end:end + 3]:               # skip at most a blank or two
        if not probe.strip():
            continue
        m = _DEFINES.match(probe.strip())
        return (m.group(1) or m.group(2)) if m else ""
    return ""


def chunks() -> list[tuple[str, int, str]]:
    out = []
    here = pathlib.Path(__file__)
    for p in sorted(here.parent.rglob("*.py")):
        if p == here:
            continue                     # self-reference is noise, not rationale
        try:
            src = p.read_text(encoding="utf-8")
            lines = src.split("\n")
            # the name goes in the TEXT so it is searchable, and on the front so
            # a query for it also wins the tf tiebreak against a mere mention
            # comment blocks only: the package carries no docstrings to index
            out += [(p.name, ln, f"{_defined_below(lines, end)}\n{t}".strip())
                    for ln, end, t in _comment_blocks(src) if len(t) > 60]
        except (OSError, SyntaxError, tokenize.TokenError):
            continue
    return out


def explain(db=None, topic="") -> list[str]:
    ch = chunks()
    df: collections.Counter[str] = collections.Counter()
    for _, _, t in ch:
        df.update(set(_words(t)))
    n = len(ch)
    q = set(_words(topic))
    scored = sorted(((sum(math.log(n / df[w]) for w in q & set(_words(t))), f, ln, t)
                     for f, ln, t in ch), reverse=True, key=lambda x: x[0])
    if not scored or scored[0][0] <= 0:
        return [f"nothing in the source explains {topic!r}.",
                "try a constant by name: MATERIAL_SWING, MOVE_EV, DOWN_SHARE, SEED"]
    s, f, ln, t = scored[0]
    out = [f"{f}:{ln}", ""] + t.splitlines()
    others = [f"{f2}:{l2}" for s2, f2, l2, _ in scored[1:4] if s2 > 0]
    if others:
        out += ["", f"also relevant: {', '.join(others)}"]
    return out


# ── the registry ─────────────────────────────────────────────────────────────
# label -> (function, arg schema). The ONLY thing that has to exist for both
# the menu and the matcher to work, and the exact shape an LLM router would
# want if fixed phrasing ever proves too rigid.
QUESTIONS: list[tuple[str, Callable[..., list[str]], dict]] = [
    ("what am I losing money on",       underwater,       {}),
    ("what should I order this week",   buy_worklist,     {}),
    ("what capital is sitting dead",    dead_stock,       {}),
    ("when do prices change next",      next_repricing,   {}),
    ("what is on my shelf",             shelf_summary,    {}),
    ("what is this drug",               drug_info,        {"ndc": str}),
    ("what has this drug's price done", price_history,    {"ndc": str}),
    ("will this drug's price move",     move_probability, {"ndc": str}),
    ("when should I reorder this drug", reorder_plan,     {"ndc": str}),
    # off: breakeven question -- uncomment to restore
    # ("what script size breaks even",    breakeven_qty,    {"ndc": str}),
    ("find a drug by description",      find_drug,        {"phrase": str}),
    # off: model/why questions -- uncomment to restore
    # ("what model is deciding this",     model_report,     {}),
    # ("why does the tool say that",      explain,          {"topic": str}),
]


# One CSV column group per per-drug chat question, in the order the chat asks
# them. Values, not the chat's formatted lines: a CSV gets sorted and filtered.
DRUG_COLUMNS = [
    "ndc", "description", "nadac_file", "nadac_unit", "pricing_unit",   # what is this drug
    "rate_class", "otc", "effective_date",
    "weeks_priced", "weeks_changed", "first_price", "price_change_pct",  # price history
    "price_age_days", "move_odds", "days_to_likely_move", "expected_price",  # will it move
    "on_hand", "monthly", "days_of_supply", "reorder_action", "reorder_why",  # reorder
    "order_qty", "timing_value",
    "your_cost", "breakeven",                                           # break-even
]


def write_drugs(path: str, db=None) -> int:
    import csv

    from pharmacli import enrich
    from pharmacli.cli import (MATERIAL_SWING, breakeven, days_to_reprice,
                               expected_price, move_odds, reorder_advice)
    from pharmacli.output import open_output
    from pharmacli.store import connect, watched

    conn = connect(db or load().db)
    try:
        ndcs = watched(conn)
    finally:
        conn.close()
    fee = load().dispensing_fee
    log.info("writing %d drugs to %s", len(ndcs), path)
    with open_output(pathlib.Path(path)) as fh:
        # restval="" -- a drug with no quantities or no cost leaves those blank
        w = csv.DictWriter(fh, fieldnames=DRUG_COLUMNS, restval="", lineterminator="\n")
        w.writeheader()
        for n in ndcs:
            # the same lookup and the same functions the chat's answers use, so a
            # row and the chat cannot disagree about a number
            got = _drug(db, n)
            if not got:                         # on the shelf, never in the panel
                w.writerow({"ndc": n})
                continue
            d, cost, (oh, mo), age = got
            price, rc = float(d["unit_price"]), d["rate_class"]
            row = {"ndc": n, "description": d["description"], "nadac_file": d["as_of_date"],
                   "nadac_unit": d["unit_price"], "pricing_unit": d["pricing_unit"],
                   "rate_class": rc, "otc": d["otc"], "effective_date": d["effective_date"],
                   "price_age_days": age, "move_odds": move_odds(age),
                   "days_to_likely_move": round(days_to_reprice(age)),
                   "expected_price": f"{expected_price(price, age, rc):.5f}",
                   "on_hand": "" if oh is None else f"{oh:g}",
                   "monthly": "" if mo is None else f"{mo:g}"}
            h = enrich.history(n, db)
            if h:
                first, last = h[0][1], h[-1][1]
                row |= {"weeks_priced": len(h),
                        "weeks_changed": sum(p != prev for (_, p), (_, prev) in zip(h[1:], h)),
                        "first_price": f"{first:.5f}",
                        "price_change_pct": f"{(last - first) / first * 100:.2f}" if first else ""}
            if oh is not None and mo:
                a = reorder_advice(oh, mo, age, cost if cost is not None else price, rc,
                                   min_swing=MATERIAL_SWING)
                row |= {"days_of_supply": "" if a["days_left"] is None else f"{a['days_left']:.1f}",
                        "reorder_action": a["action"], "reorder_why": a["why"],
                        "order_qty": a["qty"], "timing_value": f"{a['saving']:.2f}"}
            if cost is not None:
                # breakeven() returns a STRING ("never", "ANY qty") -- written as-is
                row |= {"your_cost": f"{cost:.5f}",
                        "breakeven": breakeven(cost, price, fee, d["pricing_unit"])}
            w.writerow(row)
    return len(ndcs)


def menu() -> list[str]:
    out = ["", "pharmacli ask \"<question>\" -- what this can answer:", ""]
    out += [f"  {i:>2}. {label}" for i, (label, _, _) in enumerate(QUESTIONS, 1)]
    out += ["",
            # "  a question starting with \"why\" always goes to the source comments.",  # off: model/why questions -- uncomment to restore
            "  looking up a drug by name? ask \"find a drug by description\"."]
    return out


def match(question: str) -> list[tuple[float, str, Callable[..., list[str]], dict]]:
    qw = _key(question)
    ranked = []
    for label, fn, schema in QUESTIONS:
        lw = _key(label)
        hit = qw & lw
        # A shared CONTENT word is required to select an entry at all. difflib
        # only breaks ties between real matches (and orders the "did you mean"
        # list) -- it can never push an unrelated question over the cutoff.
        score = (len(hit) / len(lw) if lw and hit else 0.0)
        score += difflib.SequenceMatcher(None, question.lower(), label).ratio() * 0.01
        ranked.append((score, label, fn, schema))
    return sorted(ranked, reverse=True, key=lambda x: x[0])


CUTOFF = 0.45


def answer(question: str, db=None, **kw) -> tuple[str, list[str]]:
    # off: model/why questions -- uncomment to restore
    # "why ..." is always the rationale lane. No label will ever out-score a
    # literal question word, and explain() is the only entry taking free text.
    # if _words(question)[:1] == ["why"]:
    #     return "", explain(db, kw.get("topic") or question)

    ranked = match(question)
    score, label, fn, schema = ranked[0]
    if score < CUTOFF:
        near = [l for s, l, _, _ in ranked[:3] if s > 0.2]   # real matches only
        out = [f"no question matches {question!r}."]
        if near:
            out += ["", "did you mean:"] + [f"  {l}" for l in near]
        return "", out + menu()

    # only ask for the args this question actually needs
    args = {}
    for name, cast in schema.items():
        val = kw.get(name)
        if val in (None, ""):
            # a drug question genuinely cannot proceed without the drug; a
            # "top 3" style knob can, so it falls through to the callee.
            if cast is not str:
                continue
            if not sys.stdin.isatty():
                return label, [f"{label!r} needs --{name.replace('_', '-')}"]
            val = input(f"{name}? ").strip()
        if val not in (None, ""):
            args[name] = cast(val)
    body = fn(db=db, **args)
    while body and not body[0]:          # sections bring their own leading blank
        body.pop(0)
    return label, body
