import difflib
import functools
import inspect
import logging
import pathlib
import re
import sys
from collections.abc import Callable

# claims.py imports nothing heavier than store and enrich, so no lazy import
from pharmacli.claims import generic_savings
from pharmacli.config import load

log = logging.getLogger(__name__)

# identifiers and words, lowercased
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


def _words(s: str) -> list[str]:
    return _WORD.findall(s.lower())


# Filler that every label shares. Without stripping these, "what dose of
# lisinopril should I give" scores against "what is on my shelf" on the word
# "what" alone and gets ANSWERED -- a clinical question served a shelf total.
# Matching has to turn on content words only.
_STOP = frozenset((
    "what is the a an on my of do does this that i am are should would can it to for "
    "in and me you how when which be by at with was were will not no yes get give"
).split())


def _key(s: str) -> set:
    return set(_words(s)) - _STOP


# An NDC inside a sentence: 11 digits, or hyphenated / spaced. Shared by the
# browser chat and the documents lane.
NDC = re.compile(r"\d[\d\s-]{9,}\d")


# Words only a package insert or a reimbursement document can answer. Checked
# before the price matcher: "what are the contraindications for this drug"
# shares "drug" with "what is this drug" and scored 1.0 against it. Measured on
# every label in QUESTIONS and every phrase in test_ask: none contain these, so
# price routing is unchanged. A Jaccard rescore was tried first and still sent
# the contraindications question to prices at 0.50.
_DOC_WORDS = frozenset((
    "dose doses dosage dosing contraindication contraindications interaction "
    "interactions warning warnings boxed adverse reaction reactions side effects "
    "pregnancy pregnant lactation breastfeeding pediatric geriatric renal hepatic "
    "kidney liver impairment overdose overdosage indication indications indicated "
    "storage store stored label insert crush crushed administration missed "
    "cms medicaid mac appeal appeals pbm reimbursement methodology regulation statute cfr"
).split())


# Pricing-flavoured subset of _DOC_WORDS: "store", "medicaid" and friends show
# up in real price questions too ("which drugs in my store are losing money"),
# so unlike the rest of _DOC_WORDS they must not override a strong price match
# outright -- lane() only lets them win when that match is thin.
_BIZ_WORDS = frozenset("store label mac appeal appeals medicaid reimbursement pbm cms".split())


# ── the money lane ───────────────────────────────────────────────────────────
# Each of these returns LINES, so the caller only has to print. The three
# worklist questions slice cli.format_worklists rather than re-wording it, so
# `ask` and `today` cannot drift.

def _worklists(db, within=14, dead_days=90, shelf=None):
    from pharmacli.cli import worklists
    return worklists(db or load().db, within, dead_days, shelf=shelf)


def _section(w, tag, within=14, dead_days=90) -> list[str]:
    from pharmacli.cli import format_worklists

    lines = format_worklists(w, within, dead_days)
    i = next(n for n, s in enumerate(lines) if s.startswith(tag))
    j = next((n for n in range(i + 1, len(lines)) if lines[n] == ""), len(lines))
    return lines[:4] + lines[i:j]


def underwater(db=None, shelf=None) -> list[str]:
    w = _worklists(db, shelf=shelf)
    return [w["error"]] if "error" in w else _section(w, "APPEAL")


# Fixed horizons. `today --within` and `reorder --within` already take the
# knob; a question interface the browser cannot pass flags to does not need it.
def buy_worklist(db=None, shelf=None) -> list[str]:
    w = _worklists(db, shelf=shelf)
    return [w["error"]] if "error" in w else _section(w, "BUY")


def dead_stock(db=None, shelf=None) -> list[str]:
    w = _worklists(db, shelf=shelf)
    return [w["error"]] if "error" in w else _section(w, "CLEAR")


def next_repricing(db=None) -> list[str]:
    from datetime import date

    from pharmacli.cli import next_reprice

    today = date.today()
    nxt = next_reprice(today)
    return [f"next repricing file {nxt} ({(nxt - today).days} days from today)",
            "the NADAC monthly lands on the first Monday on or after the 15th;",
            "the weekly file carrying it is dated that Wednesday."]


def shelf_summary(db=None, shelf=None) -> list[str]:
    from pharmacli.store import connect, costs, newest_week, stock

    conn = connect(db or load().db)
    try:
        week = newest_week(conn)
        mine, qty = shelf if shelf else (costs(conn), stock(conn))
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


@functools.lru_cache(maxsize=1)
def _embedder():
    from sentence_transformers import SentenceTransformer

    from pharmacli.nlp import EMB_MODEL
    return SentenceTransformer(EMB_MODEL)


def keyword_search(texts: list[str], q: str, k: int = 5) -> list[str]:
    ql = q.strip().lower()
    terms = [t for t in re.split(r"\s+", ql) if t]
    hits = []
    for t in texts:
        key = t.lower()
        if key.startswith(ql):
            hits.append((1, t))
        elif terms and all(term in key for term in terms):
            hits.append((2, t))
    hits.sort(key=lambda x: (x[0], x[1]))
    return [t for _, t in hits[:k]]


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
        out.append("  scores are similarity, NOT confidence that this is your drug.")
    except Exception:                       # no [analysis,nlp] extras, or no cached vectors
        names = keyword_search(texts, phrase)
        out = [f"keyword matches for {phrase!r}"
               " (install .[analysis,nlp] to match by meaning):"]
        out += [f"  {t}   {ndcs[t]}" for t in names] or ["  nothing matched"]
    return out


def stocked_recalls(db=None, shelf=None) -> list[str]:
    import httpx

    from pharmacli.safety import shelf_recalls

    try:
        return shelf_recalls(db, shelf=shelf)
    except httpx.HTTPError as exc:           # the chat must answer, not crash, offline
        return [f"openFDA recall search failed: {exc}"]


def unpriced(db=None, shelf=None) -> list[str]:
    w = _worklists(db, shelf=shelf)
    if "error" in w:
        return [w["error"]]
    if not w["unpriced"]:
        return [f"every one of your {w['priced']:,} positions has a price "
                f"in the {w['week']} file"]
    out = [f"{len(w['unpriced']):,} of {w['priced']:,} NDCs have no price in the "
           f"{w['week']} file -- CMS stopped pricing them, or the NDC is wrong:"]
    out += [f"  {n}" for n in w["unpriced"][:20]]
    if len(w["unpriced"]) > 20:
        out.append(f"  ... {len(w['unpriced']) - 20} more")
    return out


# What a restock changes: which NDCs the file adds, which it drops, and where a
# cost moved. Ranked by money, like every other worklist.
def shelf_changes(db=None, shelf=None) -> list[str]:
    from pharmacli.store import connect, costs, newest_week, stock

    if not shelf:
        return ["attach an inventory CSV first, or run: pharmacli check FILE.csv"]
    mine, qty = shelf
    conn = connect(db or load().db)
    try:
        saved_cost, saved_qty = costs(conn), stock(conn)
        week = newest_week(conn)
        named = dict(conn.execute("SELECT ndc, description FROM nadac WHERE as_of_date = ?",
                                  (week,))) if week else {}
    finally:
        conn.close()

    def name(ndc: str) -> str:
        return str(named.get(ndc, "(not in the NADAC file)"))[:34]

    def head(tag: str, n: int, one: str, many: str) -> str:
        return f"{tag:<9}{n:,} {one if n == 1 else many}"

    added = sorted(((mine[n] * (qty.get(n, (None, None))[1] or 0), n)
                    for n in mine if n not in saved_cost), reverse=True)
    dropped = sorted(((saved_cost[n] * (saved_qty.get(n, (None, None))[0] or 0), n)
                      for n in saved_cost if n not in mine), reverse=True)
    moved = sorted(((abs(mine[n] - saved_cost[n]) * (qty.get(n, (None, None))[1] or 0),
                     mine[n] - saved_cost[n], n)
                    for n in mine if n in saved_cost and mine[n] != saved_cost[n]),
                   reverse=True)

    out = [f"comparing the attached file with your saved shelf "
           f"({len(saved_cost):,} positions)", ""]
    out.append(head("ADD", len(added), "NDC in the file is not on your saved shelf",
                    "NDCs in the file are not on your saved shelf"))
    for spend, n in added[:5]:
        out.append(f"         {name(n):<35} {n}   ${spend:,.2f}/month")
    out.append("")
    out.append(head("REMOVE", len(dropped), "NDC on your saved shelf is not in the file",
                    "NDCs on your saved shelf are not in the file"))
    for value, n in dropped[:5]:
        out.append(f"         {name(n):<35} {n}   ${value:,.2f} on hand")
    out.append("")
    out.append(head("COST", len(moved), "cost changed", "costs changed"))
    for impact, delta, n in moved[:5]:
        out.append(f"         {name(n):<35} {n}   {delta:+.5f}/unit  ${impact:,.2f}/month")
    return out


# The chips the chat offers after an upload. Each count comes from the same
# function that answers the question, so a chip cannot disagree with its answer.
def suggestions(db=None, shelf=None) -> list[dict]:
    from pharmacli.claims import generics
    from pharmacli.store import connect, costs

    w = _worklists(db, shelf=shelf)
    if "error" in w:
        return []
    conn = connect(db or load().db)
    try:
        saved = len(costs(conn))
    finally:
        conn.close()
    cheaper = generics(db or load().db, shelf)
    cheaper = [] if isinstance(cheaper, str) else cheaper

    # (dollars, question, hint) -- dollars rank the chips, 0.0 sorts last
    ranked = [
        (w["bleed"], "what am I losing money on",
         f"{len(w['under']):,} positions · ${w['bleed']:,.2f}/month") if w["under"] else None,
        (w["capital"], "what capital is sitting dead",
         f"${w['capital']:,.2f} tied up") if w["dead"] else None,
        (sum((b - g) * (m or 0) for _, _, b, g, m in cheaper),
         "which brands have cheaper generics",
         f"{len(cheaper):,} lines") if cheaper else None,
        (0.0, "what should I order this week",
         f"{len(w['buy']):,} due inside 14 days") if w["buy"] else None,
        (0.0, "what changed since my saved shelf",
         f"against {saved:,} saved positions") if shelf and saved else None,
        (0.0, "which drugs have no NADAC price",
         f"{len(w['unpriced']):,} of {w['priced']:,}") if w["unpriced"] else None,
    ]
    live = sorted((r for r in ranked if r), key=lambda r: -r[0])
    return [{"q": q, "hint": hint} for _, q, hint in live[:4]]


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
    ("find a drug by description",      find_drug,        {"phrase": str}),
    # Worded against the whole-word matcher: "which drugs are recalled" and "which
    # brands have a cheaper generic" reach these, and no existing label collides.
    # "any recalls on my shelf" still reaches the shelf summary ("recalls" is not
    # "recalled"); stemming would move every other question's routing too.
    ("which stocked drugs are recalled",   stocked_recalls,  {}),
    ("which brands have cheaper generics", generic_savings,  {}),
    # answered about an attached CSV as readily as about the saved shelf
    # NOT worded with "NADAC" or "price": those are the words reimbursement
    # questions use, and this label stole two of them from the documents lane
    ("which stocked drugs are missing a price", unpriced,      {}),
    ("what changed since my saved shelf",   shelf_changes,    {}),
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


def lane(question: str) -> str:
    words = _key(question)
    if words & (_DOC_WORDS - _BIZ_WORDS):
        return "documents"
    score, label, _, _ = match(question)[0]
    if score < CUTOFF:
        return "documents"
    # a pricing-flavoured word ("store", "medicaid") only claims the question
    # when the price match is thin; "which drugs in my store are losing money"
    # shares two words with its label and stays a price question
    if words & _BIZ_WORDS and len(words & _key(label)) < 2:
        return "documents"
    return "price"


def _no_match(question: str, ranked: list) -> list[str]:
    near = [l for s, l, _, _ in ranked[:3] if s > 0.2]   # real matches only
    out = [f"no question matches {question!r}."]
    if near:
        out += ["", "did you mean:"] + [f"  {l}" for l in near]
    return out + menu()


def _documents(question: str) -> list[str] | None:
    import asyncio

    from pharmacli import rag

    result = asyncio.run(rag.ask_once(question))
    if result.get("error"):
        # no corpus, or Chroma / Ollama not running: fall back to the menu
        # rather than fail a CLI that answers prices without either
        log.warning("documents lane unavailable: %s", result["error"])
        return None
    return rag.format_lines(result)


DOCUMENTS_DOWN = ("answers from package inserts and reimbursement documents"
                   " are unavailable right now -- docker compose up -d chroma ollama")


def answer(question: str, db=None, shelf=None, **kw) -> tuple[str, list[str]]:
    ranked = match(question)
    score, label, fn, schema = ranked[0]
    if lane(question) == "documents":
        got = _documents(question)
        if got is not None:
            return "documents", got
        out = _no_match(question, ranked)
        out.insert(1, DOCUMENTS_DOWN)
        return "", out

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
    # an uploaded CSV answers only the questions that read the shelf
    if shelf and "shelf" in inspect.signature(fn).parameters:
        args["shelf"] = shelf
    body = fn(db=db, **args)
    while body and not body[0]:          # sections bring their own leading blank
        body.pop(0)
    return label, body
