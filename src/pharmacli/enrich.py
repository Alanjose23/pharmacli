# openFDA lookups: turn an NDC into an actual drug

# variable lookup lengths(4-4-2, 5-3-2, 5-4-1)
# nadac = 11, OpenFDA variable lengths

import logging
import sqlite3

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
            # confirm package
            packages = {pad(p["package_ndc"]) for p in item.get("packaging", [])
                        if p.get("package_ndc")}
            if ndc in packages:
                # return dict with information
                return {
                    "generic_name": item.get("generic_name", ""),
                    "brand_name": item.get("brand_name", ""),
                    "dosage_form": item.get("dosage_form", ""),
                    "route": ", ".join(item.get("route", []) or []),
                    "labeler": item.get("labeler_name", ""),
                    "marketing_category": item.get("marketing_category", ""),
                    "n_ingredients": len(item.get("active_ingredients", []) or []),
                }
    return None


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
