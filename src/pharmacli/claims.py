# Reimbursement checks: which claims were paid below the NADAC benchmark or
# below what the drug cost you, and where brand stock has a cheaper generic.

import bisect
import collections
import csv
import random
from datetime import date, timedelta
from pathlib import Path

from pharmacli.config import load
from pharmacli.enrich import history
from pharmacli.output import open_output
from pharmacli.store import connect, costs, newest_week

# Illustrative, and named so. `claims sample` tells you to replace them with
# your contract fees; nothing here is a real payer's rate.
SAMPLE_PAYERS = {"Medicaid (sample)": 10.64, "PBM A (sample)": 0.50, "PBM B (sample)": 1.25}
MAX_SAMPLE = 2000
UNDERPAID_FIELDS = ("date", "rx", "ndc", "payer", "qty", "paid", "nadac_unit", "fee",
                    "benchmark", "shortfall", "cost_unit", "below_cost_loss")
DAW = ("a purchasing and prescriber conversation, not a substitution -- "
       "dispense-as-written prescriptions stay as written.")


def _write_csv(path: str, header: tuple, rows) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def sample(db: str, claims_path: str, payers_path: str, days: int = 30, seed: int = 7) -> int:
    # ponytail: synthetic claims so the check can be built and demoed before a
    # real claims export exists; swap in the export, keep the columns
    conn = connect(db)
    try:
        week = newest_week(conn)
        rows = conn.execute(
            "SELECT w.ndc, w.monthly, MAX(CAST(n.unit_price AS REAL)) FROM watch w"
            " JOIN nadac n ON n.ndc = w.ndc AND n.as_of_date = ?"
            " WHERE w.monthly > 0 GROUP BY w.ndc ORDER BY w.ndc", (week,)).fetchall()
    finally:
        conn.close()
    # one generator, fixed seed, dates counted back from the panel's own newest
    # file: the same database always yields the same bytes
    rng = random.Random(seed)
    end = date.fromisoformat(week) if week else date.today()
    claims: list[tuple] = []
    for ndc, monthly, nadac in rows:
        for _ in range(max(1, round(monthly / 30))):
            if len(claims) == MAX_SAMPLE:
                break
            payer = rng.choice(list(SAMPLE_PAYERS))
            paid = nadac * 30 * rng.uniform(0.80, 1.10) + SAMPLE_PAYERS[payer]
            claims.append(((end - timedelta(days=rng.randrange(days))).isoformat(),
                           100000 + len(claims), ndc, 30, f"{paid:.2f}", payer))
    if claims:
        _write_csv(payers_path, ("payer", "dispensing_fee"), SAMPLE_PAYERS.items())
        _write_csv(claims_path, ("date", "rx", "ndc", "qty", "paid", "payer"), claims)
    return len(claims)


def read_claims(path: str) -> tuple[list[dict], list[tuple[int, str]]]:
    rows, skipped = [], []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for line, r in enumerate(csv.DictReader(fh), start=2):
            try:
                day = date.fromisoformat(r["date"].strip()).isoformat()
                ndc, qty, paid = r["ndc"].strip(), float(r["qty"]), float(r["paid"])
            except (KeyError, ValueError, AttributeError) as exc:
                skipped.append((line, f"unreadable ({exc})"))
                continue
            if not (len(ndc) == 11 and ndc.isdigit()) or qty <= 0 or paid < 0:
                skipped.append((line, "needs an 11-digit ndc, qty > 0 and paid >= 0"))
                continue
            rows.append({"date": day, "rx": (r.get("rx") or "").strip(), "ndc": ndc,
                         "qty": qty, "paid": paid, "payer": (r.get("payer") or "").strip()})
    return rows, skipped


def read_payers(path: str | None) -> dict[str, float]:
    if not path:
        return {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return {r["payer"].strip(): float(r["dispensing_fee"]) for r in csv.DictReader(fh)}


def check(db: str, claims: list[dict], fees: dict[str, float]) -> dict:
    default_fee = load().dispensing_fee
    conn = connect(db)
    try:
        cost = costs(conn)
    finally:
        conn.close()
    prices: dict[str, list[tuple[str, float]]] = {}
    priced, unpriced = [], 0
    for c in claims:
        # ponytail: one history() per distinct NDC; one SQL query if claim files get big
        if c["ndc"] not in prices:
            prices[c["ndc"]] = history(c["ndc"], db)
        h = prices[c["ndc"]]
        if not h:
            unpriced += 1
            continue
        # the price in effect on the fill date: the newest file on or before it
        # (the oldest one when the claim predates the panel)
        nadac = h[max(bisect.bisect_right(h, c["date"], key=lambda x: x[0]) - 1, 0)][1]
        fee = fees.get(c["payer"], default_fee)
        benchmark = nadac * c["qty"] + fee
        unit_cost = cost.get(c["ndc"])
        loss = unit_cost * c["qty"] - (c["paid"] - fee) if unit_cost is not None else 0.0
        priced.append(c | {"nadac_unit": nadac, "fee": fee, "benchmark": round(benchmark, 2),
                           "shortfall": round(benchmark - c["paid"], 2), "cost_unit": unit_cost,
                           "below_cost_loss": round(max(loss, 0.0), 2)})
    return {"claims": priced, "unpriced": unpriced}


def underpaid(result: dict) -> list[dict]:
    return sorted((r for r in result["claims"] if r["shortfall"] > 0), key=lambda r: -r["shortfall"])


def report(result: dict, skipped: int = 0, top: int = 25) -> list[str]:
    rows, under = result["claims"], underpaid(result)
    below = [r for r in rows if r["below_cost_loss"] > 0]
    out = [f"{len(rows):,} claims priced, {skipped:,} skipped, {result['unpriced']:,} unpriced",
           f"underpaid vs NADAC + fee   {len(under):,} claims  ${sum(r['shortfall'] for r in under):,.2f}",
           f"paid below your cost       {len(below):,} claims  ${sum(r['below_cost_loss'] for r in below):,.2f}"]
    if not under:
        return out + ["", "clean: no claim was paid below the NADAC benchmark"]

    out += ["", f"{'payer':<24}{'claims':>8}{'underpaid':>11}{'shortfall':>12}"]
    for payer in sorted({r["payer"] for r in rows}):
        mine = [r for r in under if r["payer"] == payer]
        count = sum(1 for r in rows if r["payer"] == payer)
        out.append(f"{payer[:23]:<24}{count:>8}{len(mine):>11}"
                   f"{sum(r['shortfall'] for r in mine):>12,.2f}")
    out += ["", f"{'date':<11}{'rx':<8}{'ndc':<12}{'payer':<20}{'paid':>9}{'benchmark':>11}{'short':>9}"]
    out += [f"{r['date']:<11}{r['rx']:<8}{r['ndc']:<12}{r['payer'][:19]:<20}"
            f"{r['paid']:>9.2f}{r['benchmark']:>11.2f}{r['shortfall']:>9.2f}" for r in under[:top]]
    worst = collections.Counter()
    for r in under:
        worst[r["ndc"]] += r["shortfall"]
    return out + ["", f"next: pharmacli docs appeal {worst.most_common(1)[0][0]}"]


def write_underpaid(result: dict, path: str) -> int:
    rows = underpaid(result)
    with open_output(Path(path)) as fh:
        w = csv.DictWriter(fh, fieldnames=UNDERPAID_FIELDS, extrasaction="ignore",
                           lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def generics(db: str, shelf: tuple[dict, dict] | None = None) -> list[tuple] | str:
    # NADAC files carry the corresponding generic's price on brand rows, so the
    # saving needs no Orange Book: it is already in the panel
    cheaper = (" FROM nadac n WHERE n.as_of_date = ? AND n.rate_class IN ('B', 'B-ANDA')"
               " AND n.generic_price != '' AND CAST(n.generic_price AS REAL) > 0"
               " AND CAST(n.unit_price AS REAL) > CAST(n.generic_price AS REAL)")
    conn = connect(db)
    try:
        week = newest_week(conn)
        if week is None:
            return "no NADAC data. pharmacli fetch"
        if shelf is not None:                  # an uploaded CSV, not the saved shelf
            mine, qty = shelf
            if not mine:
                return "no inventory recorded. pharmacli watch import data/pharmacy_inventory.csv"
            found = conn.execute(
                "SELECT n.ndc, n.description, CAST(n.unit_price AS REAL),"
                " CAST(n.generic_price AS REAL)" + cheaper
                + " AND n.ndc IN (%s)" % ",".join("?" * len(mine)), [week, *mine]).fetchall()
            rows = [(*r, qty.get(r[0], (None, None))[1]) for r in found]
        else:
            if not conn.execute("SELECT 1 FROM watch LIMIT 1").fetchone():
                return "no inventory recorded. pharmacli watch import data/pharmacy_inventory.csv"
            rows = conn.execute(
                "SELECT n.ndc, n.description, CAST(n.unit_price AS REAL),"
                " CAST(n.generic_price AS REAL), w.monthly"
                " FROM nadac n JOIN watch w ON w.ndc = n.ndc"
                + cheaper[len(" FROM nadac n"):], (week,)).fetchall()
    finally:
        conn.close()
    # dollars a month where volume is recorded, the per-unit gap otherwise
    return sorted(rows, key=lambda r: ((r[2] - r[3]) * (r[4] or 0), r[2] - r[3]), reverse=True)


def generic_lines(rows: list[tuple], top: int = 25) -> list[str]:
    if not rows:
        return ["clean: no brand on your shelf has a cheaper NADAC generic", DAW]
    out = [f"{'drug':<34}{'ndc':<13}{'brand':>10}{'generic':>10}{'gap':>10}{'$/month':>11}"]
    for ndc, desc, brand, gen, monthly in rows[:top]:
        per_month = "" if monthly is None else f"{(brand - gen) * monthly:,.2f}"
        out.append(f"{str(desc)[:33]:<34}{ndc:<13}{brand:>10.4f}{gen:>10.4f}"
                   f"{brand - gen:>10.4f}{per_month:>11}")
    total = sum((b - g) * (m or 0) for _, _, b, g, m in rows)
    return out + ["", f"{len(rows):,} brand positions have a cheaper generic, "
                      f"${total:,.2f}/month at recorded volumes", DAW]


def generic_savings(db: str | None = None,
                    shelf: tuple[dict, dict] | None = None) -> list[str]:
    got = generics(db or load().db, shelf)
    return [got] if isinstance(got, str) else generic_lines(got)
