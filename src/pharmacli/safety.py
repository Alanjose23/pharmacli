# Label safety tools: FDA recalls that touch the shelf, what changed when a label
# was updated, a label's counseling section word for word, and what labels say
# about a list of drugs taken together.

import difflib
import re
from datetime import date, timedelta

import httpx

from pharmacli import docs, rag
from pharmacli.config import load
from pharmacli.enrich import pad

RECALL_URL = "https://api.fda.gov/drug/enforcement.json"
_PACKAGE_NDC = re.compile(r"\b\d{4,5}-\d{3,4}-\d{1,2}\b")
LOTS = "recalls are lot-specific: check the lot numbers against your stock."
COUNSEL = "verbatim from the FDA label; give the patient the FDA Medication Guide where one exists."
CAVEAT = ("quotes label text only, not a clinical decision. A pair with no quoted "
          "interaction does not mean the drugs are safe together.")
MAX_DRUGS = 6          # 15 pairs, ~10 minutes of generation on this CPU
_CLASS = {"Class I": 0, "Class II": 1, "Class III": 2}


# ── recalls ──────────────────────────────────────────────────────────────────

def fetch_recalls(client: httpx.Client, days: int = 90, today: date | None = None) -> list[dict]:
    end = today or date.today()
    # a date window, not one query per NDC: ~285 recalls fit in one request,
    # where 5,000 NDC lookups would exhaust openFDA's keyless daily quota
    search = f"report_date:[{end - timedelta(days=days):%Y%m%d} TO {end:%Y%m%d}]"
    found: list[dict] = []
    while True:
        r = client.get(RECALL_URL, params={"search": search, "limit": 1000, "skip": len(found)})
        if r.status_code == 404:                   # openFDA's "no matches"
            return found
        r.raise_for_status()
        body = r.json()
        found += body["results"]
        # openFDA refuses skip past 25,000
        if (not body["results"] or len(found) >= body["meta"]["results"]["total"]
                or len(found) >= 25000):
            return found


def recall_ndcs(recall: dict) -> tuple[set[str], set[str]]:
    # package NDCs named in the free text, and product codes (labeler + product,
    # the first nine digits) that cover every pack size of a recalled product
    text = f"{recall.get('product_description', '')} {recall.get('code_info', '')}"
    packages = {n for n in map(pad, _PACKAGE_NDC.findall(text)) if n}
    products = {n[:9] for n in (pad(f"{p}-00") for p in
                                (recall.get("openfda") or {}).get("product_ndc", [])) if n}
    return packages, products


def shelf_matches(recalls: list[dict], shelf: dict) -> list[tuple[dict, list[str]]]:
    hits = []
    for rec in recalls:
        packages, products = recall_ndcs(rec)
        # openfda.product_ndc lists every strength in the application: a Focalin
        # XR 5 mg recall carried all eight. When the text names the recalled
        # packages, match every pack size of THOSE products only (47 recalls /
        # 369 shelf NDCs -> 33 / 43 on the real shelf); the openfda list is the
        # fallback for a recall whose text names no NDC
        keys = {p[:9] for p in packages} or products
        found = sorted(n for n in shelf if n[:9] in keys)
        if found:
            hits.append((rec, found))
    hits.sort(key=lambda h: h[0].get("report_date", ""), reverse=True)
    hits.sort(key=lambda h: _CLASS.get(h[0].get("classification", ""), 3))   # stable: class, then newest
    return hits


def recall_lines(hits: list[tuple[dict, list[str]]], shelf: dict, checked: int, days: int) -> list[str]:
    if not hits:
        return [f"clean: no recalls in the last {days} days touch your shelf "
                f"({checked:,} recalls checked)"]
    out = []
    for rec, found in hits:
        stock = ", ".join(n if shelf[n] is None else f"{n} (on hand {shelf[n]:g})" for n in found)
        out += [f"{rec.get('recall_number')}  {rec.get('classification')}  {rec.get('status')}  "
                f"{rec.get('report_date')}  {rec.get('recalling_firm')}",
                f"  product  {rec.get('product_description', '')[:160]}",
                f"  reason   {rec.get('reason_for_recall', '')[:160]}",
                f"  shelf    {stock}",
                f"  lots     {rec.get('code_info', '')[:160]}", ""]
    return out + [f"{len(hits)} of {checked:,} recalls touch your shelf. {LOTS}"]


def shelf_recalls(db: str | None = None, days: int = 90,
                  shelf: tuple[dict, dict] | None = None) -> list[str]:
    from pharmacli.store import connect, stock

    if shelf is not None:                      # an uploaded CSV needs no database
        on_hand = {n: oh for n, (oh, _) in shelf[1].items()}
    else:
        conn = connect(db or load().db)
        try:
            on_hand = {n: oh for n, (oh, _) in stock(conn).items()}
        finally:
            conn.close()
    if not on_hand:
        return ["no NDCs on watch. pharmacli watch import data/pharmacy_inventory.csv"]
    with httpx.Client(timeout=load().timeout) as client:
        recalls = fetch_recalls(client, days)
    return recall_lines(shelf_matches(recalls, on_hand), on_hand, len(recalls), days)


# ── label changes ────────────────────────────────────────────────────────────

def _section_text(path) -> dict[str, list[str]]:
    # merged by heading, so a table-of-contents line and its real section count once
    out: dict[str, list[str]] = {}
    for title, body in rag.sections(rag.parse(str(path))):
        out.setdefault(title, []).extend(ln for _, ln in body)
    return out


def label_digest(prev, new, cap: int = 20) -> list[str]:
    old, cur = _section_text(prev), _section_text(new)
    order = list(cur) + [t for t in old if t not in cur]           # document order
    order.sort(key=lambda t: not t.startswith(("WARNING:", "BOXED WARNING")))   # boxed warnings first
    out = []
    for title in order:
        before, after = old.get(title), cur.get(title)
        if before == after:
            continue
        name = title or "(before the first heading)"
        if before is None or after is None:
            out.append(f"{'added' if before is None else 'removed'} section: {name}")
            continue
        diff = [d for d in difflib.unified_diff(before, after, lineterm="", n=0)
                if not d.startswith(("---", "+++", "@@"))]
        out.append(f"changed: {name}")
        out += [f"  {d}" for d in diff[:cap]]
        if len(diff) > cap:
            out.append(f"  ... {len(diff) - cap} more")
    return out or ["no text changes (the new file differs only in layout)"]


# ── counseling handout ───────────────────────────────────────────────────────

def handout(drug: str, corpus: dict, docs_dir: str = docs.DOCS_DIR) -> str:
    ids, _ = rag.resolve(drug, corpus)
    if not ids:
        raise LookupError(f"no label in the corpus for {drug}")
    label = next(e for e in corpus["inserts"] if e["doc_id"] == ids[0])
    found = [(t, body) for t, body in rag.sections(rag.parse(str(docs.pdf_path(ids[0], docs_dir))))
             if t.startswith("17 ") and body]
    if not found:
        raise LookupError(f"{label['title']} has no section 17 (older label format)")
    # the longest one: the table of contents names section 17 with no body
    title, body = max(found, key=lambda s: len(s[1]))
    lines: list[str] = []
    for _, ln in body:
        # a line ending in a hyphen continues on the next, as chunk_fixed joins it
        if lines and lines[-1].endswith("-"):
            lines[-1] += ln
        else:
            lines.append(ln)
    first, last = body[0][0], body[-1][0]
    others = [e["title"] for e in corpus["inserts"] if e["doc_id"] in ids[1:]]
    md = [f"# Counseling points: {label['title']}", "",
          f"label {label['label_date']} · pages {first}-{last}", "", *lines, "",
          f"Source: {label['title']} · {title} · p.{first}-{last} · label {label['label_date']}"]
    if others:
        md.append(f"Other labels matching {drug!r}: {', '.join(others)}")
    return "\n".join(md + ["", f"_{COUNSEL}_"]) + "\n"


# ── interactions ─────────────────────────────────────────────────────────────

async def interactions(r: rag.Rag, drugs: list[str]):
    known = {d: rag.resolve(d, r.corpus)[0] for d in drugs}
    for d, ids in known.items():
        if not ids:
            yield f"not checked: {d} (no label in the corpus)"
    for i, a in enumerate(drugs):
        for b in drugs[i + 1:]:
            if not (known[a] or known[b]):
                continue
            result = await rag.collect(r, f"drug interactions between {a} and {b}")
            if result["error"]:
                raise ConnectionError(result["error"])
            yield ""
            yield f"== {a} + {b}"
            for line in rag.format_lines(result):
                yield line
    yield ""
    yield CAVEAT
