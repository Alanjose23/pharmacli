# writes fetched NADAC records into a SQLite file, and works out what is already
# there so an interrupted run can carry on instead of starting over

import logging
import sqlite3
from datetime import date
from collections.abc import Sequence

from pharmacli.models.medicine import Medicine

log = logging.getLogger(__name__)

# fixed schema to include nonfetched items.
#
# pharmacy_type is deliberately NOT a column. It held "C/I" on all 1,028,250
# rows fetched -- NADAC only surveys chain and independent retail pharmacies, so
# the indicator is constant by design and carries zero information. Medicine
# still parses it, because the CSV round-trip has to write all 12 columns. An
# existing database keeps the vestigial column until the next --fresh.
SCHEMA = ("CREATE TABLE IF NOT EXISTS nadac ("
          " ndc TEXT,"
          " description TEXT,"
          " unit_price TEXT,"
          " pricing_unit TEXT,"
          " effective_date TEXT,"
          " as_of_date TEXT,"
          " otc TEXT,"
          " rate_class TEXT,"
          " generic_price TEXT,"
          " generic_effective_date TEXT,"
          " explanation_code TEXT,"
          " UNIQUE(ndc, effective_date, as_of_date))")


# the pharmacy's own stock list
WATCH = ("CREATE TABLE IF NOT EXISTS watch ("
         " ndc     TEXT PRIMARY KEY,"
         " added   TEXT NOT NULL,"
         " cost    REAL,"
         " on_hand REAL,"
         " monthly REAL)")


# openFDA pack sizes, cached. The mapping is near-static and openFDA allows
# 1,000 requests a day without a key, so a 5,000-NDC shelf cannot be refetched
# per run. `looked` is stored even on a miss: ~3 NDCs in 20 are not in the
# openFDA directory, and without it every run would retry all of them forever.
PACKAGE = ("CREATE TABLE IF NOT EXISTS package ("
           " ndc       TEXT PRIMARY KEY,"
           " pack_size REAL,"
           " pack_desc TEXT,"
           " looked    TEXT NOT NULL)")


# opens file and makes sure that it exists
def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(SCHEMA)
    conn.execute(WATCH)
    conn.execute(PACKAGE)
    # Every pharmacist-facing command filters one weekly file out of ~1M rows.
    # The UNIQUE(ndc, effective_date, as_of_date) autoindex cannot serve that --
    # as_of_date is its third column -- so those queries were full table scans.
    # Measured: today 0.58s -> 0.35s, margin and reorder both
    # ~1.8x, for 19.6MB (14% of the file). Worth it for interactive commands;
    # the cost to `fetch` is invisible next to the network.
    conn.execute("CREATE INDEX IF NOT EXISTS nadac_week ON nadac(as_of_date)")
    # sqlite adding cost, on hand, and monthly values
    for col in ("cost REAL", "on_hand REAL", "monthly REAL"):
        try:
            conn.execute(f"ALTER TABLE watch ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass      # already there
    conn.commit()
    return conn


# writes records and tallies how much are introduced
def insert(conn: sqlite3.Connection, records: Sequence[Medicine]) -> int:
    rows = [
        (
            med.drug.ndc,
            med.drug.description,
            str(med.price.unitprice),
            med.price.unit,
            Medicine.formatdate(med.price.effective_date),
            Medicine.formatdate(med.asofdate),
            # y,n, or none
            Medicine.formatotc(med.otc),
            med.rateclass,
            # generic is valid even if there is a none value
            str(med.generic.unitprice) if med.generic else "",
            Medicine.formatdate(med.generic.effective_date) if med.generic else "",
            med.explanation,  # pharmacytype dropped here: constant
        )
        for med in records
    ]
    before = conn.total_changes
    
    conn.executemany(
        "INSERT OR IGNORE INTO nadac"
        " (ndc, description, unit_price, pricing_unit, effective_date, as_of_date, otc,"
        "  rate_class, generic_price, generic_effective_date, explanation_code)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    # committed changes and returned how much is different from previous iteration
    conn.commit()
    return conn.total_changes - before


# selects how many rows are already there
def count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM nadac").fetchone()[0])


# which page offsets still need to be fetched
def pending_offsets(conn: sqlite3.Connection, limit: int, page_size: int) -> list[int]:
    done = count(conn)
    offsets = list(range(0, limit, page_size))
    if done == 0:
        return offsets
    skip = done // page_size
    if skip:
        log.info("%d rows already stored, skipping %d pages", done, skip)
    return offsets[skip:]


# watch list, ndcs are padded so that ndcs stay consistent
def watch_add(conn: sqlite3.Connection, ndcs: Sequence[str],
              cost: float | None = None) -> int:
    rows = [(n.strip().zfill(11), date.today().isoformat(), cost)
            for n in ndcs if n.strip()]
    before = conn.total_changes
    conn.executemany("INSERT OR IGNORE INTO watch (ndc, added, cost) VALUES (?,?,?)", rows)
    conn.commit()
    return conn.total_changes - before


def watch_cost(conn: sqlite3.Connection,
               rows: Sequence[tuple[str, float, float | None, float | None]]) -> int:
    # upserts inventory file, so importing next month is an update not a duplicate, and adds ndcs later updated
    today = date.today().isoformat()
    vals = [(n.strip().zfill(11), today, c, oh, mo) for n, c, oh, mo in rows]
    before = conn.total_changes
    conn.executemany(
        "INSERT INTO watch (ndc, added, cost, on_hand, monthly) VALUES (?,?,?,?,?)"
        " ON CONFLICT(ndc) DO UPDATE SET"
        "   cost    = excluded.cost,"
        "   on_hand = COALESCE(excluded.on_hand, watch.on_hand),"
        "   monthly = COALESCE(excluded.monthly, watch.monthly)", vals)
    conn.commit()
    return conn.total_changes - before


def costs(conn: sqlite3.Connection) -> dict[str, float]:
    # ndc into aquisition cost per unit for ndcs that have this
    return {n: c for n, c in
            conn.execute("SELECT ndc, cost FROM watch WHERE cost IS NOT NULL")}


def stock(conn: sqlite3.Connection) -> dict[str, tuple[float | None, float | None]]:
    
    # inventory file is allowed to carry cost only
    return {n: (oh, mo) for n, oh, mo in
            conn.execute("SELECT ndc, on_hand, monthly FROM watch")}


def watch_remove(conn: sqlite3.Connection, ndcs: Sequence[str]) -> int:
    rows = [(n.strip().zfill(11),) for n in ndcs if n.strip()]
    before = conn.total_changes
    conn.executemany("DELETE FROM watch WHERE ndc = ?", rows)
    conn.commit()
    return conn.total_changes - before


def watched(conn: sqlite3.Connection) -> list[str]:
    return [n for (n,) in conn.execute("SELECT ndc FROM watch ORDER BY ndc")]


def packages(conn: sqlite3.Connection) -> dict[str, tuple[float | None, str]]:
    return {n: (size, desc or "")
            for n, size, desc in conn.execute(
                "SELECT ndc, pack_size, pack_desc FROM package")}


def save_package(conn: sqlite3.Connection, ndc: str,
                 size: float | None, desc: str) -> None:
    conn.execute("INSERT INTO package (ndc, pack_size, pack_desc, looked)"
                 " VALUES (?,?,?,?) ON CONFLICT(ndc) DO UPDATE SET"
                 " pack_size=excluded.pack_size, pack_desc=excluded.pack_desc,"
                 " looked=excluded.looked",
                 (ndc.strip().zfill(11), size, desc, date.today().isoformat()))
    conn.commit()


def panel_fingerprint(conn: sqlite3.Connection) -> str:
    import hashlib

    h = hashlib.md5()
    for row in conn.execute(
            "SELECT ndc, description, unit_price, pricing_unit, effective_date,"
            " as_of_date, otc, rate_class, generic_price, generic_effective_date,"
            " explanation_code FROM nadac ORDER BY ndc, effective_date, as_of_date"):
        h.update(repr(row).encode())
    return h.hexdigest()


def last_priced(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT ndc, MAX(as_of_date) FROM nadac GROUP BY ndc"))


def newest_week(conn: sqlite3.Connection) -> str | None:
    # most recent date as of panel
    return conn.execute("SELECT MAX(as_of_date) FROM nadac").fetchone()[0]
