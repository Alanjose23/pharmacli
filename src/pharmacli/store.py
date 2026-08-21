# writes fetched NADAC records into a SQLite file, and works out what is already
# there so an interrupted run can carry on instead of starting over

import logging
import sqlite3
from collections.abc import Sequence

from pharmacli.models.medicine import Medicine

log = logging.getLogger(__name__)

# one table
SCHEMA = """
CREATE TABLE IF NOT EXISTS nadac (
    ndc TEXT,
    description TEXT,
    unit_price TEXT,
    pricing_unit TEXT,
    effective_date TEXT,
    as_of_date TEXT,
    otc TEXT,
    UNIQUE(ndc, effective_date, as_of_date)
)
"""


# opens file and makes sure that it exists
def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(SCHEMA)
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
        )
        for med in records
    ]
    before = conn.total_changes
    
    conn.executemany(
        "INSERT OR IGNORE INTO nadac"
        " (ndc, description, unit_price, pricing_unit, effective_date, as_of_date, otc)"
        " VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    # committed changes and returned how much is totally different from previous iteration
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
