# openFDA lookups: turn an NDC into an actual drug

# variable lookup lengths(4-4-2, 5-3-2, 5-4-1)
# nadac = 11, OpenFDA variable lengths

import logging
import sqlite3
from contextlib import closing

# httpx.
import httpx

from pharmacli.config import load as _config

log = logging.getLogger(__name__)

NDC_URL = "https://api.fda.gov/drug/ndc.json"
## needed for name, form, and route


def pad(hyphenated: str) -> str | None:
   
    # open fda to ndc 11 digit form
    parts = hyphenated.split("-")
    if len(parts) != 3:
        return None
    return parts[0].zfill(5) + parts[1].zfill(4) + parts[2].zfill(2)


def packsize(description: str) -> float | None:
    head = description.strip().split(" ", 1)[0].replace(",", "")
    try:
        n = float(head)
    except ValueError:
        return None
    return n if n > 0 else None


def _candidates(ndc: str) -> list[str]:
    # openfda product code derived from nadac ndc
    ## labeller orientation
    out = [f"{ndc[:5]}-{ndc[5:9]}"]              # already 5-4 (package)
    if ndc[0] == "0":
        out.append(f"{ndc[1:5]}-{ndc[5:9]}")     # 4-4-2 short labeler
    if ndc[5] == "0":
        out.append(f"{ndc[:5]}-{ndc[6:9]}")      # 5-3-2 short product
    return out


def lookup(ndc: str, timeout: int | None = None) -> dict | None:
    # drug metadata from OpenFDA ndc, 9 digit product search
    # search full package directly before returning and matching
    ndc = ndc.strip().zfill(11)
    timeout = timeout or _config().timeout

    candidates = _candidates(ndc)

    for product in candidates:
        try:
            params: dict[str, str | int] = {"search": f'product_ndc:"{product}"',"limit": 5}
            r = httpx.get(NDC_URL, params=params, timeout=timeout)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            log.warning("openFDA lookup failed: %s", type(exc).__name__)
            return None
        if r.status_code == 404:
            continue           # openFDA returns 404 for no matches 
        if r.status_code != 200:
            log.warning("openFDA HTTP %d for %s", r.status_code, product)
            continue

        for item in r.json().get("results", []):
            # This already walked every package to confirm the NDC matches, then
            # threw the packaging away. Keep the matched one: NADAC prices per
            # UNIT, but a wholesaler sells bottles, so "order 1,500 EA" is not an
            # order and "3 x 500 ct" is.
            match = next((pk for pk in item.get("packaging", [])
                          if pk.get("package_ndc") and pad(pk["package_ndc"]) == ndc), None)
            if match:
                return {
                    "generic_name": item.get("generic_name", ""),
                    "brand_name": item.get("brand_name", ""),
                    "dosage_form": item.get("dosage_form", ""),
                    "route": ", ".join(item.get("route", []) or []),
                    "labeler": item.get("labeler_name", ""),
                    "marketing_category": item.get("marketing_category", ""),
                    "n_ingredients": len(item.get("active_ingredients", []) or []),
                    "pack_desc": match.get("description", ""),
                    "pack_size": packsize(match.get("description", "")),
                }
    return None


def history(ndc: str, db: str | None = None) -> list[tuple[str, float]]:
    ndc = ndc.strip().zfill(11)
    with closing(sqlite3.connect(db or _config().db)) as conn:
        return [(d, float(p)) for d, p in conn.execute(
            "SELECT as_of_date, unit_price FROM nadac"
            " WHERE ndc = ? AND unit_price != '' ORDER BY as_of_date", (ndc,))]


def local(ndc: str, db: str | None = None) -> dict | None:
    # what nadac already knows about this ndc, locally
    ndc = ndc.strip().zfill(11)
    with sqlite3.connect(db or _config().db) as conn:
        row = conn.execute(
            "SELECT description, unit_price, pricing_unit, rate_class, otc,"
            " effective_date, as_of_date FROM nadac WHERE ndc = ?"
            " ORDER BY as_of_date DESC LIMIT 1", (ndc,)).fetchone()
    if row is None:
        return None
    keys = ("description", "unit_price", "pricing_unit", "rate_class", "otc",
            "effective_date", "as_of_date")
    return dict(zip(keys, row))
