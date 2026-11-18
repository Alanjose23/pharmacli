# What a pharmacist reviews for one patient: which label each drug matches,
# what the labels say about each pair of drugs, the label sections the patient's
# indicators point at, and the shelf checks for their drugs.
#
# Three rules run through all of it. Everything shown carries its source, and
# what cannot be cited is withheld as "needs manual review". Uncertainty moves
# toward caution, never toward a milder answer. And the tool quotes labels: the
# pharmacist decides.

import asyncio
import functools
import hashlib
import itertools
import json
import re
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from pharmacli import docs, patients, rag

# ── which label a typed drug name means ──────────────────────────────────────

@dataclass(frozen=True)
class Drug:
    key: str                    # the first word of the generic name: "potassium"
    name: str                   # how to write it: "potassium chloride"
    labels: tuple[dict, ...]    # its corpus entries; a drug can have two labels


@dataclass(frozen=True)
class Labels:
    # what a check reads: the document list, the class terms, and where the PDFs are
    corpus: dict
    classes: dict
    docs_dir: str = docs.DOCS_DIR


def drugs(corpus: dict) -> dict[str, Drug]:
    # One drug per first word of a generic name, which is what rag.resolve keys
    # on: the corpus's 50 labels are 45 drugs this way (two potassium chloride
    # labels, two atorvastatin, ...).
    grouped: dict[str, list[dict]] = {}
    for e in corpus["inserts"]:
        grouped.setdefault(docs.drug_key(e), []).append(e)
    return {key: Drug(key, min((e["generic"].split(",")[0].lower() for e in es), key=len),
                      tuple(es)) for key, es in grouped.items()}


def labeled(corpus: dict) -> dict[str, str]:
    return {key: d.name for key, d in drugs(corpus).items()}


def suggestions(corpus: dict) -> list[str]:
    # what the entry form offers: every generic name, and a brand where the
    # label is titled with one. Each of these matches exactly one drug.
    return sorted({n.split(",")[0].lower() for e in corpus["inserts"]
                   for n in (e["generic"], e["title"])})


def name_words(corpus: dict) -> dict[str, str]:
    # the words a typed name is matched on, each with its drug: what the entry
    # form needs to flag an unmatched name as it is typed, the way match() will
    return {e[field].split()[0].lower(): docs.drug_key(e)
            for e in corpus["inserts"] for field in ("generic", "title")}


def match(name: str, corpus: dict) -> str | None:
    # The drug a typed name means, or None when it has no label or could mean
    # more than one. Never a guess: an unmatched name is kept on the profile
    # and shown as "no label, not checked".
    typed = " ".join(name.lower().split())
    for d in drugs(corpus).values():
        if any(typed in (d.name, e["generic"].lower(), e["title"].lower()) for e in d.labels):
            return d.key
    ids, _ = rag.resolve(name, corpus)           # the resolution `ask` and `docs handout` use
    keys = {docs.drug_key(e) for e in corpus["inserts"] if e["doc_id"] in ids}
    return keys.pop() if len(keys) == 1 else None


def tag(rows: list[dict], corpus: dict) -> list[dict]:
    # a patient's drug rows, each with the drug it matched and that drug's labels
    known = drugs(corpus)
    out = []
    for row in rows:
        key = match(row["name"], corpus)
        out.append(row | {"drug": key, "label": ", ".join(e["title"] for e in known[key].labels)
                          if key else None})
    return out


# ── the text of a label, by numbered section ─────────────────────────────────

class NoLabelText(LookupError):
    pass


# The section names 21 CFR 201.57 fixes for a label in the current format, by
# number. Matching the name as well as the number is what tells "5 WARNINGS AND
# PRECAUTIONS" from a "5 MG TABLETS" line or a numbered list inside a section.
_PLR = dict(enumerate(("INDICATIONS DOSAGE DOSAGE CONTRAINDICATIONS WARNINGS ADVERSE DRUG USE "
                       "DRUG OVERDOSAGE DESCRIPTION CLINICAL NONCLINICAL CLINICAL REFERENCES "
                       "HOW PATIENT").split(), 1))
_TOP = re.compile(r"(\d{1,2}) ([A-Z]+)\b.*")
_SUB = re.compile(r"(\d{1,2})\.\d")
BOXED, WHOLE = "boxed", "all"


def sections(lines: list[rag.Line]) -> list[tuple[str, str, list[rag.Line]]]:
    # -> (section number, its heading, its lines) for each numbered section that
    # has text. The Highlights and the table of contents fall away: the first is
    # a summary we do not quote, and the second has headings with nothing under
    # them. An older label has no numbered sections, and comes back whole.
    out: list[tuple[str, str, list[rag.Line]]] = []
    major = ""
    for page, text in lines:
        head = rag.heading(text) or ""
        top = _TOP.fullmatch(head)
        sub = _SUB.match(head)
        if top and _PLR.get(int(top.group(1))) == top.group(2):
            major = top.group(1)
            out.append((major, head, []))
        elif major and head.startswith(("WARNING:", "BOXED WARNING")):
            major = BOXED
            out.append((major, head, []))
        elif sub and sub.group(1) == major:
            out.append((major, head, []))
        elif out:
            out[-1][2].append((page, text))       # body text, a list item included
    found = [s for s in out if s[2]]
    if {"4", "5", "7"} & {m for m, _, _ in found}:
        return found
    return [(WHOLE, "", lines)]


@functools.lru_cache(maxsize=128)
def _label(doc_id: str, version: int, docs_dir: str) -> tuple:
    # keyed by version too, so a refreshed label is read again
    path = docs.pdf_path(doc_id, docs_dir)
    if not path.exists():
        raise NoLabelText(f"no label PDF for {doc_id} in {docs_dir} -- pharmacli docs fetch")
    return tuple(sections(rag.parse(str(path))))


def _cite(entry: dict, heading: str, first: int, last: int) -> str:
    # the citation every document answer already uses: document · section · page · label date
    return rag.cite({"title": entry["title"], "section": heading, "page_start": first,
                     "page_end": last, "label_date": entry.get("label_date", "")})


# ── how a label names another drug ───────────────────────────────────────────

_CYP = re.compile(r"cytochrome p450 (\w+) (induc|inhibit)\w*")


def _plain(text: str) -> str:
    # "Opioid Agonists [EPC]" -> "opioid agonist"
    words = re.findall(r"[a-z0-9]+", re.sub(r"\[.*?\]", "", text).lower())
    return re.sub(r"(?<=[a-z]{3})s$", "", " ".join(words))


def _phrase(text: str) -> str:
    # a class phrase as a pattern: any punctuation between its words, and a plural
    return r"\b" + r"\W+".join(map(re.escape, _plain(text).split())) + r"(?:s|es)?\b"


def _class_patterns(cls: str, aliases: dict[str, list[str]]) -> list[str]:
    out = [_phrase(cls), *map(_phrase, aliases.get(_plain(cls), []))]
    cyp = _CYP.fullmatch(_plain(cls))
    if cyp:
        # openFDA writes "Cytochrome P450 3A4 Inducers"; labels write "strong
        # CYP3A4 inducers", "inducers of CYP3A4", "induces CYP3A4"
        iso, verb = cyp.groups()
        out += [rf"\bcyp\W*{iso}\w*\W+(?:\w+\W+)?{verb}",
                rf"\b{verb}\w*\W+(?:of\W+)?(?:\w+\W+)?cyp\W*{iso}"]
    return out


def terms(drug: Drug, classes: dict) -> tuple[re.Pattern, re.Pattern | None]:
    # -> (the drug's names, its pharmacologic classes) as patterns to search a
    # label for. The second is None when no class is on file for the drug: its
    # name can still be searched, but "the labels say nothing" cannot be
    # concluded from that alone.
    names = {e["title"].split()[0].lower() for e in drug.labels}
    for e in drug.labels:
        # each ingredient of a combination: "losartan", "hydrochlorothiazide"
        names |= {part.split()[0].lower() for part in e["generic"].split(" AND ")}
    entry = (classes.get("drugs") or {}).get(drug.key) or {}
    aliases = {_plain(k): v for k, v in (classes.get("aliases") or {}).items()}
    kinds = [p for cls in entry.get("pharm_class") or [] for p in _class_patterns(cls, aliases)]
    kinds += [_phrase(t["term"]) for t in entry.get("terms") or []]
    # a name matches as a prefix: "gabapentin" is named by "gabapentinoids"
    return (re.compile("|".join(rf"\b{re.escape(n)}" for n in sorted(names)), re.I),
            re.compile("|".join(kinds), re.I) if kinds else None)


# ── the sentences of one label that name the other drug ──────────────────────

# Contraindications, the boxed warning, Drug Interactions, Warnings and
# Precautions, and Dosage and Administration (where dose adjustments sit) --
# the most severe source first, which is the order they are put to the model.
SEARCHED = ("4", BOXED, "7", "5", "2")
MAX_SENTENCES = 8
LONGEST = 420               # a table row has no full stop: quote a window of it
_EXAMPLES = re.compile(r"\W*Examples?\b", re.I)
# where one sentence ends: a full stop and a space, unless the stop belongs to
# an abbreviation -- a wrapped table cell reads "(e.g. , carbamazepine"
_BREAK = re.compile(r"(?<=[.!?])(?<!e\.g\.)(?<!i\.e\.)(?<!vs\.)(?<!etc\.)\s+")


def _sentences(text: str):
    # (start, end) of each sentence in the joined text of a section
    start = 0
    for stop in _BREAK.finditer(text):
        yield start, stop.start()
        start = stop.end()
    if start < len(text):
        yield start, len(text)


def mentions(entry: dict, own: re.Pattern, name: re.Pattern, kind: re.Pattern | None,
             docs_dir: str) -> tuple[list[dict], str]:
    # -> (sentences naming the other drug or its class, a citation of what was
    # searched). A plain text search: no model, no embedding.
    found: list[dict] = []
    parts = [s for s in _label(entry["doc_id"], entry["version"], docs_dir)
             if s[0] in SEARCHED or s[0] == WHOLE]
    for major, heading, lines in parts:
        text, starts = rag.join(lines)
        spans = list(_sentences(text))
        quoted = 0                               # how far the last quote reached
        for i, (start, end) in enumerate(spans):
            sentence = text[start:end]
            hit, by = None, "name"
            for m in name.finditer(sentence):
                # "potassium" inside this label's own "losartan potassium" is
                # the drug naming itself, not the other drug
                if not re.search(rf"(?:{own.pattern})\w*\W+$", sentence[:m.start()], re.I):
                    hit = m
                    break
            if hit is None and kind is not None:
                hit, by = kind.search(sentence), "class"
            if hit is None or start < quoted:
                continue
            at = start + hit.start()             # where in the section the match is
            table = bool(_EXAMPLES.match(sentence))
            nxt = sentence.find("Clinical Impact") if table else -1
            title = _next_row(sentence[:nxt]) if nxt > 0 else -1
            if table and 0 <= title <= hit.start():
                # The PDF runs the next row's title on from the last example
                # ("Examples: A, B Diuretics Clinical Impact: ..."), and the match
                # is in that title. It is the next row that is about this drug,
                # so quote from there, as an ordinary sentence -- not the
                # Intervention of the row above, which is about something else.
                start += title
                table = False
            if table:
                # A label's interaction table reads Clinical Impact, Intervention,
                # Examples: the drug is named in the last row and what to do
                # about it is in the row before. Quote from the Intervention to
                # the end of the examples, and stop where the next row begins.
                end = start + (title if title >= 0 else nxt if nxt > 0 else len(sentence))
                head = text.rfind("Intervention", max(0, start - 700), start)
                rows = text[start:end].strip()
                if head >= 0:
                    told = text[head:start].strip()
                    if len(told) > LONGEST:
                        told = told[:told.rfind(" ", 0, LONGEST)] + " …"
                    rows, start = f"{told} {rows}", head
                if len(rows) > 2 * LONGEST:
                    rows = rows[:rows.rfind(" ", 0, 2 * LONGEST)] + " …"
                quote, quoted = rows, end
            elif end - start > LONGEST:
                cut = text.find(" ", max(start, at - 160)) if at - 160 > start else start
                stop = text.rfind(" ", cut, min(end, at + 260)) if at + 260 < end else end
                quote = ("… " if cut > start else "") + text[cut:stop].strip() \
                    + (" …" if stop < end else "")
                start, quoted = cut, stop
            else:
                # A label often names the drug in one sentence and says what to
                # do in the next ("... produces potassium retention. Closely
                # monitor potassium ..."), so the next one is quoted with it.
                if i + 1 < len(spans) and spans[i + 1][1] - start <= LONGEST:
                    end = spans[i + 1][1]
                quote, quoted = text[start:end].strip(), end
            page = rag.page_at(starts, start)
            found.append({"major": major, "text": quote, "by": by,
                          "cite": _cite(entry, heading, page, page)})
    pages = [p for _, _, lines in parts for p, _ in lines]
    if not pages:
        where = "no Contraindications, Warnings, Drug Interactions or Dosage text found"
    elif parts[0][0] == WHOLE:
        where = "the whole label (older format, no numbered sections)"
    else:
        numbers = sorted({m for m, _, _ in parts if m != BOXED}, key=int)
        where = "sections " + ", ".join(numbers) \
            + (" and the boxed warning" if any(m == BOXED for m, _, _ in parts) else "")
    span = {"page_start": min(pages), "page_end": max(pages)} if pages \
        else {"page_start": 0, "page_end": 0}
    searched = rag.cite({"title": entry["title"], "section": where + " searched",
                         "label_date": entry.get("label_date", "")} | span)
    return found, searched


def _next_row(examples: str) -> int:
    # "Examples: Cyclobenzaprine, metaxalone Diuretics " -> where "Diuretics"
    # starts: the first capitalised word after the first word of the last item.
    # -1 when the examples run to their own full stop.
    last = max(examples.rfind(","), examples.find(":")) + 1
    words = list(re.finditer(r"\S+", examples[last:]))
    return next((last + w.start() for w in words[1:] if w.group()[0].isupper()), -1)


def ranked(found: list[dict]) -> list[dict]:
    # A sentence that names the drug before one that names only its class: the
    # class terms are wide, and must not crowd the drug's own name out of the
    # few sentences the model sees. Within each, the most severe source first.
    return sorted(found, key=lambda s: (s["by"] != "name", SEARCHED.index(s["major"])
                                        if s["major"] in SEARCHED else 0))


# ── the model's small job, and the checks on its answer ──────────────────────

# D9: a closed set, most severe first. The three statuses below are not
# categories: they keep a failed or missing check from looking like a result.
CATEGORIES = ("Do not use together", "Avoid or adjust the dose", "Monitor",
              "No interaction stated in the labels")
MANUAL, NOT_CHECKED, PENDING = "Needs manual review", "Not checked", "Pending"
NOT_SAFE = "No interaction stated in the labels, which is not the same as safe."
# how a report sorts: a failed check above everything, an unresolved pair above
# the one result that could be mistaken for reassurance
ORDER = (MANUAL, *CATEGORIES[:3], NOT_CHECKED, PENDING, CATEGORIES[3])

PROMPT = "4"                # bump to re-ask every pair: a new prompt, a new way of finding
                            # and quoting the sentences, or a new check on the answer
# Written for a 3B model: the words that decide each category are spelled out,
# with one made-up example each, and the sentence is chosen before the
# category so the answer has to rest on something. Version 1 asked only for a
# category and a number, and the model answered "A, sentence 1" to everything.
SYSTEM = (
    "You sort sentences taken from one drug's FDA label. Each sentence mentions a second "
    "drug or its drug class. Decide what the label tells a pharmacist about a patient "
    "taking the two drugs together.\n"
    "Step 1: pick the ONE sentence that gives the strongest instruction about taking the "
    "two together.\n"
    "Step 2: give that sentence a category, from the words it uses:\n"
    "A = it says the two are contraindicated together or must not be used together.\n"
    "B = it says to avoid the combination, or to reduce, increase, double, halve or "
    "otherwise change a dose because of it.\n"
    "C = it says to monitor or use caution, or it only describes a risk or a changed "
    "drug level.\n"
    "D = no sentence is about taking the two drugs together.\n"
    "Examples:\n"
    "\"Zeta is contraindicated in patients taking omega.\" is A.\n"
    "\"Reduce the zeta dose by half when it is given with omega.\" is B.\n"
    "\"Omega may raise zeta levels; monitor for drowsiness.\" is C.\n"
    "\"Zeta was studied in patients with diabetes.\" is D.\n"
    "Reply with exactly two lines and nothing else:\n"
    "SENTENCE: <the number of the sentence you picked>\n"
    "CATEGORY: <A, B, C or D>")
_PICKED = re.compile(r"SENTENCE\W*(\d+)", re.I)
_LETTER = re.compile(r"CATEGORY\W*([A-D])\b", re.I)


def build_prompt(drug: Drug, other: Drug, shown: list[dict]) -> list[dict]:
    # Only label text and the two drugs' names from the corpus go in. No
    # patient field is passed to this function, so none can reach the model,
    # and nothing a person typed can steer it.
    numbered = "\n".join(f"[{n}] {s['text']}" for n, s in enumerate(shown, 1))
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Label: {drug.name}\nOther drug: {other.name}\n\n"
                                        f"{numbered}\n\nSENTENCE and CATEGORY:"}]


# PROVISIONAL AND UNREVIEWED (D12): the words a label uses when it forbids a
# combination, and when it changes a dose for one. They never set a category.
# They catch the two ways the model was measured to go wrong: calling a
# sentence "do not use together" that does not say so, and reading a sentence
# as milder than its own words. Either sends the pair to a pharmacist.
_WORDING = (
    re.compile(r"contraindicated|must not|do not (?:use|take|give|administer|co-?administer|"
               r"combine)|should not be (?:used|taken|given|administered|co-?administered|"
               r"combined)", re.I),
    re.compile(r"\bavoid|not recommended|\breduc\w* (?:\w+ ){0,3}dos|\bdoubl|\bhalf\b|\bhalve"
               r"|dos\w* (?:\w+ ){0,2}(?:adjust|reduc|modif)|adjust\w* (?:\w+ ){0,3}dos"
               r"|(?:increase|decrease|lower)\w* (?:\w+ ){0,3}dos|reserve concomitant"
               r"|\blimit (?:\w+ ){0,2}(?:dos|duration)", re.I),
)


# not wording about this combination: a cross-reference ("[see Contraindications
# (4)]") and a dose change that is ruled out ("no dose adjustment is warranted")
_ASIDE = re.compile(r"\[[^\]]*\]|\b(?:no|not|without)\b[^.;]{0,40}?adjust\w*", re.I)


def worded(text: str) -> int:
    # how severe the sentence's own words are, as an index into CATEGORIES:
    # 0 forbids, 1 avoids or adjusts, 2 neither
    text = _ASIDE.sub(" ", text)
    return next((i for i, cue in enumerate(_WORDING) if cue.search(text)), 2)


def verdict(reply: str, shown: list[dict], other: Drug, name: re.Pattern,
            kind: re.Pattern | None) -> dict:
    # -> {"category", "evidence"} or {"manual": why}. Every way the answer can
    # be wrong ends in manual review, never in a milder category.
    picked, category = _PICKED.search(reply), _LETTER.search(reply)
    if not picked or not category:
        return {"manual": "the model did not answer with one of the categories"}
    letter, number = category.group(1).upper(), int(picked.group(1))
    if letter == "D":
        # "no interaction stated" is only ever concluded from a search that
        # found nothing -- and this one found something
        return {"manual": f"a label names {other.name} or its class, but the model read no "
                          "interaction into the sentences"}
    if not 1 <= number <= len(shown):
        return {"manual": "the model pointed at a sentence it was not given"}
    sentence = shown[number - 1]
    if not (name.search(sentence["text"]) or (kind and kind.search(sentence["text"]))):
        return {"manual": f"the sentence the model chose does not name {other.name} or its class"}
    got = "ABC".index(letter)
    if got == 0 and worded(sentence["text"]) != 0:
        return {"manual": "the model answered \"do not use together\", but the sentence it "
                          "chose does not say that"}
    # A category is never milder than the words it rests on: not the words of
    # the sentence the model chose, and not those of any sentence that names
    # the drug itself. (Sentences naming only its class are too many and too
    # loosely about this drug to set the floor unless one of them is chosen.)
    floor = min([worded(sentence["text"]),
                 *(worded(s["text"]) for s in shown if s["by"] == "name")])
    if floor < got:
        return {"manual": f"a sentence naming {other.name} or its class is worded as "
                          f"\"{CATEGORIES[floor].lower()}\", more severe than the model's answer"}
    return {"category": CATEGORIES[got], "evidence": sentence}


async def check(r: rag.Rag, a: Drug, b: Drug, labels: Labels) -> dict:
    # What the labels of two drugs say about using them together. Looks in
    # both: a's labels for b, and b's labels for a.
    sides: list[dict] = []
    searched: list[str] = []
    unknown: list[str] = []
    for drug, other in ((a, b), (b, a)):
        own, _ = terms(drug, labels.classes)
        name, kind = terms(other, labels.classes)
        if kind is None:
            unknown.append(other.name)
        found: list[dict] = []
        for entry in drug.labels:
            sentences, where = mentions(entry, own, name, kind, labels.docs_dir)
            found += sentences
            searched.append(where)
        if not found:
            continue
        shown = ranked(found)[:MAX_SENTENCES]
        reply = "".join([p.get("message", {}).get("content", "") async for p in rag.chat_stream(
            r.http, r.cfg.ollama_url, r.cfg.llm, build_prompt(drug, other, shown))])
        side = verdict(reply, shown, other, name, kind)
        if "manual" in side:
            # a failed check still has the label's own words to offer: the
            # sentences found, most severely worded first, with no category
            # claimed for them. They are what the pharmacist will read.
            side["found"] = sorted(shown, key=lambda s: worded(s["text"]))[:3]
        sides.append(side)

    manual = [s["manual"] for s in sides if "manual" in s]
    read = sorted((s for s in sides if "category" in s),
                  key=lambda s: CATEGORIES.index(s["category"]))
    if manual:
        status, reason = MANUAL, "; ".join(manual)
    elif read:
        status, reason = read[0]["category"], ""       # the more severe of the two readings
    else:
        status, reason = CATEGORIES[3], ""             # a search of both found nothing
    if unknown and status != CATEGORIES[0]:
        # the search was incomplete, so nothing milder than "do not use" stands
        status = MANUAL
        reason = "; ".join(filter(None, [reason, "no pharmacologic class is on file for "
                                         f"{' or '.join(unknown)}, so only the name was "
                                         "searched for"]))
    quotes = [{"category": s["category"], "quote": s["evidence"]["text"],
               "cite": s["evidence"]["cite"]} for s in read]
    quotes += [{"category": None, "quote": f["text"], "cite": f["cite"]}
               for s in sides for f in s.get("found", [])]
    return {"status": status, "reason": reason, "searched": searched, "quotes": quotes}


# ── the cache, and the queue that is derived from it ─────────────────────────

def _key(a: Drug, b: Drug, labels: Labels, model: str) -> tuple[str, str, str, str, str]:
    # both label versions, the prompt and the model: change any and the pair is asked again
    a, b = sorted((a, b), key=lambda d: d.key)
    versions = "|".join(",".join(f"{e['doc_id']}:{e['version']}" for e in d.labels)
                        for d in (a, b))
    known = labels.classes.get("drugs") or {}
    shaping = json.dumps([PROMPT, SYSTEM, SEARCHED, MAX_SENTENCES, LONGEST, known.get(a.key),
                          known.get(b.key), labels.classes.get("aliases")], sort_keys=True)
    return a.key, b.key, versions, hashlib.sha256(shaping.encode()).hexdigest()[:16], model


def cached(conn, a: Drug, b: Drug, labels: Labels, model: str) -> dict | None:
    return patients.pair_get(conn, *_key(a, b, labels, model))


def pending(conn, labels: Labels, model: str) -> list[tuple[Drug, Drug]]:
    # The queue is not a table. It is every pair some patient's drugs need,
    # minus the pairs already answered for the current labels, prompt and
    # model -- so it resumes by itself after an interruption, and a refreshed
    # label puts its pairs back in.
    known = drugs(labels.corpus)
    taken: dict[str, set[str]] = {}
    for pid, name in patients.drug_names(conn):
        key = match(name, labels.corpus)
        if key:
            taken.setdefault(pid, set()).add(key)
    wanted = {pair for keys in taken.values()
              for pair in itertools.combinations(sorted(keys), 2)}
    return [(known[a], known[b]) for a, b in sorted(wanted)
            if cached(conn, known[a], known[b], labels, model) is None]


async def work(r: rag.Rag, db: str, labels: Labels, busy=None, limit: int | None = None,
               progress=None) -> int:
    # One pair at a time: the model serves one request at a time, and `busy`
    # lets a pharmacist's live question go ahead of the pairs still waiting.
    # Each answer is stored as it lands, so stopping loses at most one pair.
    with closing(patients.connect(db)) as conn:
        todo = pending(conn, labels, r.cfg.llm)
    done = 0
    for a, b in todo[:limit]:
        while busy and busy():
            await asyncio.sleep(0.5)
        try:
            result = await check(r, a, b, labels)
        except NoLabelText:
            continue                    # stays pending until `docs fetch` brings the PDF
        with closing(patients.connect(db)) as conn:
            patients.pair_put(conn, *_key(a, b, labels, r.cfg.llm), result)
        done += 1
        if progress:
            progress(a, b, result, len(todo) - done)
    return done


# ── label sections matched to a patient's indicators ─────────────────────────

# PROVISIONAL AND UNREVIEWED (D12): which reading brings up which label text.
# A trigger only chooses what to show. The guidance is the label's own words,
# with the patient's value beside it. Blood pressure, potassium, weight and
# allergies are recorded and displayed, and trigger nothing in this version.
TRIGGERS: tuple[dict[str, Any], ...] = (
    {"indicator": "egfr", "test": "below", "threshold": 60.0, "topic": "renal",
     "source": "KDIGO 2024 Clinical Practice Guideline for CKD (Kidney Int 2024;105(4S)): "
               "GFR categories G3a to G5 are below 60 mL/min/1.73 m2"},
    {"indicator": "crcl", "test": "below", "threshold": 60.0, "topic": "renal",
     "source": "FDA guidance for industry, Pharmacokinetics in Patients with Impaired Renal "
               "Function (March 2024), Table 1: 30 to below 60 mL/minute is moderate impairment"},
    {"indicator": "alt", "test": "above", "threshold": 40.0, "topic": "hepatic",
     "source": "ABIM Laboratory Test Reference Ranges (January 2025): ALT 10 to 40 U/L"},
    {"indicator": "ast", "test": "above", "threshold": 40.0, "topic": "hepatic",
     "source": "ABIM Laboratory Test Reference Ranges (January 2025): AST 10 to 40 U/L"},
    {"indicator": "pregnant", "test": "is", "threshold": "yes", "topic": "pregnancy",
     "source": "21 CFR 201.57(c)(9)(i): a label's pregnancy information is its section 8.1"},
)
# topic -> (what to call it, which numbered sections hold it)
TOPICS = {
    "renal": ("renal impairment",
              lambda major, head: major in ("2", "5", "8") and "renal" in head.lower()),
    "hepatic": ("hepatic impairment",
                lambda major, head: major in ("2", "5", "8") and "hepatic" in head.lower()),
    "pregnancy": ("pregnancy", lambda major, head: head.startswith("8.1 ")),
}
_SHOWN = {"egfr": "eGFR", "crcl": "creatinine clearance", "alt": "ALT", "ast": "AST",
          "pregnant": "pregnant"}
QUOTE = 1200                # characters of a section shown before "see the label"
READ, GENERIC, RECALL = "Label text for this patient", "Cheaper generic", "Recall"


def fired(current: dict) -> dict[str, list[dict]]:
    # topic -> the triggers a patient's current readings set off, each with why
    out: dict[str, list[dict]] = {}
    for t in TRIGGERS:
        reading = current.get(t["indicator"])
        if reading is None:
            continue
        value, name = reading["value"], _SHOWN[t["indicator"]]
        if t["test"] == "is":
            hit, shown = value == t["threshold"], f"{name}: {value}"
        else:
            hit = float(value) < t["threshold"] if t["test"] == "below" \
                else float(value) > t["threshold"]
            shown = (f"{name} {value} {patients.BOUNDS[t['indicator']][0]} is "
                     f"{t['test']} {t['threshold']:g}")
        if hit:
            out.setdefault(t["topic"], []).append(
                {"why": f"{shown} (reading of {reading['taken']})", "source": t["source"]})
    return out


def indicator_items(drug: Drug, topic: str, why: list[dict], docs_dir: str) -> list[dict]:
    # the label's own section for this topic, quoted with its pages: no model call
    what, wanted = TOPICS[topic]
    note = ("; ".join(w["why"] for w in why) + ". Trigger, provisional and not reviewed by a "
            "pharmacist: " + "; ".join(dict.fromkeys(w["source"] for w in why)) + ".")
    items = []
    for entry in drug.labels:
        quotes = []
        for major, heading, lines in _label(entry["doc_id"], entry["version"], docs_dir):
            if major in (WHOLE, BOXED) or not wanted(major, heading):
                continue
            text, _ = rag.join(lines)
            if len(text) > QUOTE:
                text = text[:text.rfind(" ", 0, QUOTE)] + " … (continues: see the label)"
            quotes.append({"quote": text, "cite": _cite(entry, heading, lines[0][0], lines[-1][0])})
        item = {"kind": "indicator", "title": f"{drug.name}: {what}", "status": READ,
                "note": note, "quotes": quotes}
        if not quotes:
            # nothing to quote is not nothing to say: the label may cover it
            # under another heading, or be in the older format
            item |= {"status": MANUAL,
                     "note": f"{entry['title']} (label {entry.get('label_date', '')}) has no "
                             f"section headed {what} that could be quoted: read the label. " + note}
        items.append(item)
    return items


# ── shelf checks, per patient ────────────────────────────────────────────────

def generic_items(mine: list[Drug], db: str) -> tuple[list[dict], str]:
    # brand packages of the patient's drugs whose NADAC row carries a cheaper
    # generic price -- the existing check, pointed at these NDCs
    from pharmacli import claims
    from pharmacli.store import connect, newest_week

    ndcs = {n: d for d in mine for e in d.labels for n in e["ndcs"]}
    if not Path(db).exists():
        return [], f"generic prices not checked: there is no price panel at {db}"
    rows = claims.generics(db, ({n: 0.0 for n in ndcs}, {})) if ndcs else []
    if isinstance(rows, str):
        return [], f"generic prices not checked: {rows}"
    with closing(connect(db)) as conn:
        week = newest_week(conn)
    quotes: dict[str, list[dict]] = {}
    for ndc, desc, brand, generic, _ in rows:
        quotes.setdefault(ndcs[ndc].name, []).append(
            {"quote": f"{desc} (NDC {ndc}): brand ${brand:.5f} a unit, generic ${generic:.5f}",
             "cite": f"NADAC file {week}"})
    return [{"kind": "generic", "title": f"{name}: a cheaper generic is priced",
             "status": GENERIC, "note": claims.DAW, "quotes": found[:5]}
            for name, found in quotes.items()], ""


@functools.lru_cache(maxsize=2)
def _recalls(day: str) -> tuple:
    # one request a day per process: a date window, never one query per drug,
    # so nothing about a patient is in what is sent
    from pharmacli import safety
    from pharmacli.config import load

    with httpx.Client(timeout=load().timeout) as client:
        return tuple(safety.fetch_recalls(client))


def recall_items(mine: list[Drug]) -> tuple[list[dict], str]:
    # FDA recalls from the last 90 days that name a package of one of these
    # drugs' labels -- the existing matching, pointed at these NDCs
    from pharmacli import safety

    ndcs = {n: d for d in mine for e in d.labels for n in e["ndcs"]}
    try:
        recent = _recalls(date.today().isoformat())
    except httpx.HTTPError as exc:
        return [], f"recalls not checked: openFDA could not be reached ({type(exc).__name__})"
    items = []
    for rec, hit in safety.shelf_matches(list(recent), dict.fromkeys(ndcs)):
        items.append({
            "kind": "recall", "status": RECALL, "note": safety.LOTS,
            "title": f"{ndcs[hit[0]].name}: FDA recall {rec.get('recall_number')}",
            "quotes": [{"quote": f"{rec.get('classification')}. {rec.get('reason_for_recall', '')}"
                                 f" Product: {rec.get('product_description', '')[:300]}",
                        "cite": f"FDA recall {rec.get('recall_number')} · reported "
                                f"{rec.get('report_date')} · {rec.get('recalling_firm')}"}]})
    return items, ""


# ── the report ───────────────────────────────────────────────────────────────

def _item_key(item: dict) -> str:
    # what a decision is recorded against. It covers what the item says, so a
    # decision does not carry over to a result that has since changed.
    said = [item["kind"], item["title"], item["status"],
            [(q["quote"], q["cite"]) for q in item["quotes"]], item.get("searched")]
    return hashlib.sha256(json.dumps(said).encode()).hexdigest()[:16]


def report(conn, pid: str, labels: Labels, model: str, price_db: str) -> dict | None:
    # Rendered from the cache: no model call, so it is quick however many pairs
    # are still waiting -- and it says how many are.
    t0 = time.perf_counter()
    profile = patients.profile(conn, pid)
    if profile is None:
        return None
    known = drugs(labels.corpus)
    tagged = tag(profile["drugs"], labels.corpus)
    mine = [known[k] for k in dict.fromkeys(d["drug"] for d in tagged if d["drug"])]
    loose = [d["name"] for d in tagged if not d["drug"]]      # no label: cannot be checked

    pairs = [pair_item(a, b, cached(conn, a, b, labels, model))
             for a, b in itertools.combinations(mine, 2)]
    names = [d.name for d in mine] + loose
    for i, name in enumerate(loose, len(mine)):
        pairs += [{"kind": "pair", "title": f"{other} + {name}", "status": NOT_CHECKED,
                   "quotes": [], "note": f"{name} has no label in the corpus"}
                  for other in names[:i]]
    pairs.sort(key=lambda p: ORDER.index(p["status"]))

    notes: list[str] = []
    others: list[dict] = []
    for topic, why in fired(profile["current"]).items():
        for drug in mine:
            try:
                others += indicator_items(drug, topic, why, labels.docs_dir)
            except NoLabelText as exc:
                # one missing PDF must not hide the other drugs' sections
                others.append({"kind": "indicator", "title": f"{drug.name}: {TOPICS[topic][0]}",
                               "status": MANUAL, "quotes": [],
                               "note": f"the label could not be read ({exc}): read it yourself."})
    for found, problem in (generic_items(mine, price_db), recall_items(mine)):
        others += found
        notes += [problem] if problem else []
    others.sort(key=lambda i: i["status"] != MANUAL)

    made = patients.decisions(conn, pid)
    items = pairs + others
    for item in items:
        if item["status"] not in (PENDING, NOT_CHECKED):     # something to accept or override
            item["key"] = _item_key(item)
            item["decision"] = made.get(item["key"])
    waiting = [p["title"] for p in pairs if p["status"] == PENDING]
    checked = sum(p["status"] not in (PENDING, NOT_CHECKED) for p in pairs)
    summary = f"{checked} of {len(pairs)} pairs checked"
    summary += f"; pending: {', '.join(waiting)}" if waiting else ""
    summary += f"; not checked, no label: {', '.join(loose)}" if loose else ""
    return {"patient": {k: profile[k] for k in ("id", "name", "dob")}, "summary": summary,
            "checked": checked, "total": len(pairs), "pending": waiting, "not_checked": loose,
            "items": items, "notes": notes,
            "ms": round((time.perf_counter() - t0) * 1000, 1)}


def item_lines(item: dict) -> list[str]:
    made = item.get("decision")
    out = [f"{item['status'].upper()}  {item['title']}"
           + (f"   [{made['verdict']} by {made['by']}]" if made else "")]
    out += [f"  {item['note']}"] if item["note"] else []
    for q in item["quotes"]:
        # a quote under a failed check carries no category: it is only the label's words
        read = f"[{q['category']}] " if q.get("category") else ""
        out += [f"  {read}\"{q['quote']}\"", f"    {q['cite']}"]
    # with nothing to quote, the citation is what was searched
    return out + [f"  searched: {s}" for s in item.get("searched", []) if not item["quotes"]]


def pair_item(a: Drug, b: Drug, got: dict | None) -> dict:
    item: dict = {"kind": "pair", "title": f"{a.name} + {b.name}", "quotes": []}
    if got is None:
        return item | {"status": PENDING, "note": "not run yet: the check is queued"}
    return item | {"status": got["status"], "quotes": got["quotes"], "searched": got["searched"],
                   "note": NOT_SAFE if got["status"] == CATEGORIES[3] else got["reason"]}


def report_lines(rep: dict) -> list[str]:
    # the report as text, for `pharmacli patients report`
    out = [f"{rep['patient']['name']} · born {rep['patient']['dob']} · synthetic patient",
           rep["summary"], ""]
    for item in rep["items"]:
        out += [*item_lines(item), ""]
    return out + rep["notes"]


# ── a patient's question ─────────────────────────────────────────────────────
# A patient gets label quotes, never advice. So a question is answered only
# when it is recognisably a general question about a label: it names a drug
# that has one, and every other word is in the short list below. That list has
# no "I", "my" or "me" and no "can", "should" or "safe", so "can I ...",
# "should I ...", "is it safe for me ..." and "I missed a dose" all fall
# outside it -- as does anything else it does not recognise. It is an allow-
# list on purpose: it does not depend on the model, and a paraphrase it has
# never seen is refused rather than let through.
_ASKING = re.compile(r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
                     r"(?:tell|show|give)\s+me\s+", re.I)
_GENERAL = frozenset((
    "what which how when where why does do is are was were the a an of for about on in to and "
    "or with by at from as than there any some all most common commonly serious possible known "
    "label labels labeled insert leaflet information section sections say says said state "
    "states stated list lists listed mention mentions describe describes medicine medicines "
    "medication medications drug drugs tablet tablets capsule capsules dose doses dosage dosing "
    "strength strengths usual maximum daily side effect effects adverse reaction reactions "
    "warning warnings boxed precaution precautions contraindication contraindications "
    "interaction interactions storage store stored storing kept keep temperature pregnancy "
    "breastfeeding lactation overdose overdosage missed ingredient ingredients inactive active "
    "use uses used indication indications treat treats treated taken food alcohol driving "
    "counseling patients adults children elderly symptoms signs long term supplied form forms"
).split())
REFUSAL = ("I can only quote what a medicine's label says in general, and I can't answer a "
           "question about your own situation. The counseling text for your medicines is on "
           "this page. Your pharmacist can answer this: you can send them the question.")
NOT_FOUND = ("I could not find that in the label. Your pharmacist can answer this: you can "
             "send them the question.")


def general_label_question(question: str, corpus: dict) -> bool:
    ids, _ = rag.resolve(question, corpus)
    if not ids:
        return False                                      # no labeled drug named
    named = {w for e in corpus["inserts"] if e["doc_id"] in ids
             for w in re.findall(r"[a-z0-9]+", f"{e['generic']} {e['title']}".lower())}
    words = re.findall(r"[a-z0-9']+", _ASKING.sub("", question.strip()).lower())
    return bool(words) and all(w in _GENERAL or w in named for w in words)


async def patient_answer(r: rag.Rag, question: str) -> dict:
    # -> {"refused", "message", "quotes"}. The gate runs first and needs no
    # service; an answer is label text as retrieved, word for word, with its
    # citation. Nothing is generated.
    if not general_label_question(question, r.corpus):
        return {"refused": True, "message": REFUSAL, "quotes": []}
    hits = await rag.retrieve(r, question, r.cfg.k, r.cfg.chunking)
    quotes = []
    for hit in hits[:2]:
        if hit.score < r.cfg.relevance_floor:
            continue
        text = hit.text
        for lead in (hit.meta["title"], hit.meta.get("section") or ""):
            # the index stores "TITLE · section · text"; only the text is the label's
            text = text.removeprefix(f"{lead} · ") if lead else text
        quotes.append({"quote": f"… {text.strip()} …", "cite": rag.cite(hit.meta)})
    if not quotes:
        return {"refused": True, "message": NOT_FOUND, "quotes": []}
    return {"refused": False, "message": "From the label, word for word:", "quotes": quotes}
