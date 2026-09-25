# The corpus behind the documents lane: which package inserts and which
# reimbursement documents, where each PDF comes from, and whether the copy on
# disk is still the current one.

import hashlib
import json
import logging
import re
import time
from contextlib import closing
from pathlib import Path

import httpx

from pharmacli.enrich import _candidates, pad

log = logging.getLogger(__name__)

CORPUS = "corpus.json"
DOCS_DIR = "data/docs"
LABEL_URL = "https://api.fda.gov/drug/label.json"
PDF_URL = "https://dailymed.nlm.nih.gov/dailymed/downloadpdffile.cfm?setId={}"
# openFDA allows 240 requests a minute without a key; a shelf walk that outran
# it would get 429s and silently skip the highest-spend drugs
PAUSE = 0.25

# Each URL was downloaded and confirmed to be a PDF on 2026-09-14. NOT here:
# DFS's 11 NYCRR 456.6 "MAC lists and appeals". That text exists only in a
# 2023 proposed rule which the 2024 final adoption left out, and quoting a
# withdrawn proposal as law is worse than having no source for it.
BUSINESS = [
    {"doc_id": "cms-nadac-methodology", "jurisdiction": "federal",
     "title": "CMS Methodology for Calculating the NADAC",
     "url": "https://www.medicaid.gov/medicaid-chip-program-information/by-topics/"
            "prescription-drugs/ful-nadac-downloads/nadacmethodology.pdf"},
    {"doc_id": "cms-nadac-qa", "jurisdiction": "federal",
     "title": "CMS NADAC Questions and Responses",
     "url": "https://www.medicaid.gov/sites/default/files/medicaid-chip-program-information/"
            "by-topics/prescription-drugs/ful-nadac-downloads/nadacqa.pdf"},
    {"doc_id": "cfr-42-447-i", "jurisdiction": "federal",
     "title": "42 CFR Part 447 Subpart I",
     "url": "https://www.govinfo.gov/content/pkg/CFR-2024-title42-vol4/pdf/"
            "CFR-2024-title42-vol4-part447-subpartI.pdf"},
    {"doc_id": "ny-phl-280-a", "jurisdiction": "NY",
     "title": "NY Public Health Law §280-a",
     "url": "https://legislation.nysenate.gov/pdf/laws/PBH280-A"},
    {"doc_id": "ny-emedny-pharmacy-policy", "jurisdiction": "NY",
     "title": "eMedNY NYRx Pharmacy Policy Guidelines",
     "url": "https://www.emedny.org/ProviderManuals/Pharmacy/PDFS/Pharmacy_Policy_Guidelines.pdf"},
    {"doc_id": "ny-emedny-pharmacy-billing", "jurisdiction": "NY",
     "title": "eMedNY NYRx Pharmacy Billing Guidelines",
     "url": "https://www.emedny.org/ProviderManuals/Pharmacy/PDFS/Pharmacy_Billing_Guidelines.pdf"},
]


def load_corpus(path: str = CORPUS) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_corpus(corpus: dict, path: str = CORPUS) -> None:
    Path(path).write_text(json.dumps(corpus, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def digest(corpus: dict) -> str:
    return hashlib.sha256(json.dumps(corpus, sort_keys=True).encode()).hexdigest()


def pdf_path(doc_id: str, docs_dir: str = DOCS_DIR) -> Path:
    # doc_id comes from corpus.json and openFDA's set_id; neither may climb out
    if not re.fullmatch(r"[A-Za-z0-9][\w-]*", doc_id):
        raise ValueError(f"unsafe doc_id: {doc_id!r}")
    return Path(docs_dir) / f"{doc_id}.pdf"


def download(url: str, dest: Path, client: httpx.Client) -> str | None:
    try:
        r = client.get(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        log.warning("download failed %s: %s", url, type(exc).__name__)
        return None
    # the status alone is not trusted: an error page can arrive as a 200
    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        log.warning("not a PDF: %s (HTTP %d)", url, r.status_code)
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    return hashlib.sha256(r.content).hexdigest()


def entry(item: dict) -> dict:
    o = item.get("openfda", {})
    t = item["effective_time"]                   # "20260812"
    return {"doc_id": item["set_id"],
            "title": (o.get("brand_name") or o.get("generic_name") or ["?"])[0].upper(),
            "generic": (o.get("generic_name") or ["?"])[0].upper(),
            "version": int(item["version"]),
            "label_date": f"{t[:4]}-{t[4:6]}-{t[6:8]}",
            "ndcs": sorted({n for n in map(pad, o.get("package_ndc", [])) if n})}


def label_for_ndc(ndc: str, client: httpx.Client) -> dict | None:
    ndc = ndc.strip().zfill(11)
    for product in _candidates(ndc):
        r = client.get(LABEL_URL, params={"search": f'openfda.product_ndc:"{product}"',
                                          "limit": 5})
        time.sleep(PAUSE)
        if r.status_code == 404:                  # openFDA's "no matches"
            continue
        r.raise_for_status()
        for item in r.json().get("results", []):
            # a product code covers every pack size; confirm this exact package
            if ndc in {pad(p) for p in item.get("openfda", {}).get("package_ndc", [])}:
                return entry(item)
    return None


def from_shelf(db: str, n: int, client: httpx.Client, docs_dir: str = DOCS_DIR) -> dict:
    from pharmacli.store import connect

    with closing(connect(db)) as conn:
        # spend, not count: the labels worth having first are the drugs the
        # pharmacy puts the most money through
        ranked = [ndc for (ndc,) in conn.execute(
            "SELECT ndc FROM watch WHERE cost IS NOT NULL AND monthly IS NOT NULL"
            " ORDER BY cost * monthly DESC")]

    inserts: list[dict] = []
    seen: set[str] = set()
    for ndc in ranked:
        if len(inserts) == n:
            break
        try:
            e = label_for_ndc(ndc, client)
        except httpx.HTTPError as exc:
            log.warning("openFDA failed for %s: %s", ndc, exc)
            continue
        # one label per generic: ten atorvastatin labellers are one drug to a reader
        if e is None or e["generic"] in seen:
            continue
        if download(PDF_URL.format(e["doc_id"]), pdf_path(e["doc_id"], docs_dir), client) is None:
            continue
        seen.add(e["generic"])
        inserts.append(e)
        log.info("%d/%d %s", len(inserts), n, e["generic"])

    business = []
    for b in BUSINESS:
        sha = download(b["url"], pdf_path(b["doc_id"], docs_dir), client)
        if sha is None:
            log.error("reimbursement document %s did not download", b["doc_id"])
            continue
        business.append({**b, "sha256": sha})
    return {"inserts": inserts, "business": business}


def fetch(corpus: dict, client: httpx.Client, docs_dir: str = DOCS_DIR) -> list[str]:
    failed = []
    for e in corpus["inserts"]:
        p = pdf_path(e["doc_id"], docs_dir)
        if not p.exists() and download(PDF_URL.format(e["doc_id"]), p, client) is None:
            failed.append(e["doc_id"])
    for b in corpus["business"]:
        p = pdf_path(b["doc_id"], docs_dir)
        # fetch only fills in missing files, like inserts above. Overwriting a
        # copy that differs would lose the old version `docs refresh` diffs against
        have = (hashlib.sha256(p.read_bytes()).hexdigest() if p.exists()
                else download(b["url"], p, client))
        if have != b["sha256"]:
            # changed upstream since corpus.json was built; refresh decides
            log.warning("%s no longer matches its recorded sha256 -- "
                        "pharmacli docs refresh", b["doc_id"])
            failed.append(b["doc_id"])
    return failed


def refresh(corpus: dict, client: httpx.Client, docs_dir: str = DOCS_DIR) -> list[str]:
    changed = []
    for i, e in enumerate(corpus["inserts"]):
        try:
            r = client.get(LABEL_URL, params={"search": f'set_id:"{e["doc_id"]}"', "limit": 1})
        except httpx.HTTPError as exc:
            log.warning("openFDA failed for %s: %s", e["doc_id"], exc)
            continue
        if r.status_code != 200:
            log.warning("openFDA HTTP %d for %s", r.status_code, e["doc_id"])
            continue
        new = entry(r.json()["results"][0])
        if new["version"] <= e["version"]:
            continue
        # the old file becomes .prev.pdf so `docs refresh` can show what changed.
        # ponytail: one previous version, not a history
        p = pdf_path(e["doc_id"], docs_dir)
        prev = p.with_suffix(".prev.pdf")
        if p.exists():
            p.replace(prev)
        if download(PDF_URL.format(e["doc_id"]), p, client):
            corpus["inserts"][i] = new
            changed.append(e["doc_id"])
        elif prev.exists():
            prev.replace(p)                  # a failed download keeps the old label, not none
    for b in corpus["business"]:
        p = pdf_path(b["doc_id"], docs_dir)
        fresh = p.with_suffix(".new.pdf")
        sha = download(b["url"], fresh, client)
        if sha and sha != b["sha256"]:
            if p.exists():
                p.replace(p.with_suffix(".prev.pdf"))
            fresh.replace(p)
            b["sha256"] = sha
            changed.append(b["doc_id"])
        else:
            fresh.unlink(missing_ok=True)
    return changed
