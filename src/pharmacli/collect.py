# fetching from API and turns into medicine objects
# four ways: one at a time, with threads, with processes, and with asyncio

import asyncio
import logging
import random
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import Any, cast

import httpx
import requests

from pharmacli.config import load
from pharmacli.err import FetchError
from pharmacli.models.medicine import Medicine
from pharmacli.models.rows import COLUMNS, RawRow, SkippedRow

log = logging.getLogger(__name__)

# CMS NADAC set
URL = "https://data.medicaid.gov/api/1/datastore/query/fbb83258-11c7-47f5-8b18-5f8e79f7e704/0"

# recirds per request
PAGE_SIZE = 100

# error handling if page fails
MAX_ATTEMPTS = 3

# API fields
FIELD_MAP = {
    "ndc_description": "NDC Description",
    "ndc": "NDC",
    "nadac_per_unit": "NADAC Per Unit",
    "effective_date": "Effective Date",
    "pricing_unit": "Pricing Unit",
    "pharmacy_type_indicator": "Pharmacy Type Indicator",
    "otc": "OTC",
    "explanation_code": "Explanation Code",
    "classification_for_rate_setting": "Classification for Rate Setting",
    "corresponding_generic_drug_nadac_per_unit": "Corresponding Generic Drug NADAC Per Unit",
    "corresponding_generic_drug_effective_date": "Corresponding Generic Drug Effective Date",
    "as_of_date": "As of Date",
}


# turing API record into dict
def to_row(record: dict[str, Any]) -> RawRow:
    row: dict[str, str] = {}
    for api_key, column in FIELD_MAP.items():
        value = record.get(api_key)
        # attaching values to medicines
        row[column] = "" if value is None else str(value)
    # NADAC codes are 11 digits
    row["NDC"] = row["NDC"].strip().zfill(11)
    # any field the API left out entirely still has to exist, because arow reads
    # all twelve by name
    for column in COLUMNS:
        row.setdefault(column, "")
    return cast(RawRow, row)


# parses a page of API records into Medicine objects
# skipped record is recorded
def parse(records: list[dict[str, Any]], offset: int) -> list[Medicine]:
    out: list[Medicine] = []
    for index, record in enumerate(records):
        result = Medicine.arow(to_row(record), offset + index)
        if isinstance(result, Medicine):
            out.append(result)
        elif isinstance(result, SkippedRow):
            log.debug("skipped record at offset %d: %s", offset + index, result.reason)
    return out


# waiting before retrying
def backoff(attempt: int) -> float:
    return 2 ** attempt + random.uniform(0, 1)


# checks whether API request is retriable
def retryable(status: int) -> bool:
    return status == 429 or status >= 500



#fetching page with requests
def fetch_page(session: requests.Session, offset: int, timeout: int) -> list[dict]:
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(
                URL, params={"limit": PAGE_SIZE, "offset": offset}, timeout=timeout
            )
            ## time out for checking if requests are working properly
            if response.status_code == 200:
                return response.json().get("results", [])
            if not retryable(response.status_code):
                raise FetchError(f"offset {offset}: HTTP {response.status_code}")
            log.warning("offset %d: HTTP %d", offset, response.status_code)
        except (requests.ConnectionError, requests.Timeout) as exc:
            # dropped connection or timeout error as a warning
            log.warning("offset %d: %s", offset, type(exc).__name__)

        # log errors after max attempt requests(make sure that I get some requests to work)
        if attempt < MAX_ATTEMPTS - 1:
            wait = backoff(attempt)
            log.warning("offset %d: retrying in %.1fs", offset, wait)
            time.sleep(wait)

    raise FetchError(f"offset {offset}: gave up after {MAX_ATTEMPTS} attempts")



# async model
async def fetch_page_async(
    client: httpx.AsyncClient, offset: int
) -> list[dict]:
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = await client.get(URL, params={"limit": PAGE_SIZE, "offset": offset})
            if response.status_code == 200:
                return response.json().get("results", [])
            if not retryable(response.status_code):
                raise FetchError(f"offset {offset}: HTTP {response.status_code}")
            log.warning("offset %d: HTTP %d", offset, response.status_code)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            log.warning("offset %d: %s", offset, type(exc).__name__)

        if attempt < MAX_ATTEMPTS - 1:
            wait = backoff(attempt)
            log.warning("offset %d: retrying in %.1fs", offset, wait)
            # specific async sleep function
            await asyncio.sleep(wait)

    raise FetchError(f"offset {offset}: gave up after {MAX_ATTEMPTS} attempts")


def fetch_sequential(pages: list[int], workers: int = 1, on_page=None) -> list[Medicine]:
    timeout = load().timeout
    out: list[Medicine] = []
    # one session on every page, once instead of per requests
    with requests.Session() as session:
        for offset in pages:
            records = parse(fetch_page(session, offset, timeout), offset)
            if on_page:
                on_page(records)
            out.extend(records)
    return out


# storing sessions
_local = threading.local()


# hands back the calling thread's own Session, creating it the first time
#
# a requests.Session is NOT safe to share between threads. it holds a connection
# pool and cookie state, and several threads mutating that at once corrupts it.
# the failures are intermittent and look like random network errors, which makes
# them miserable to track down. giving each thread its own session keeps the
# connection reuse without the sharing -- threading.local means each thread sees
# a different value under the same attribute name
def _session() -> requests.Session:
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
    return _local.session


# the unit of work handed to a thread. takes a single tuple because pool.map
# passes one argument per item
def _fetch_one_threaded(args: tuple[int, int]) -> list[Medicine]:
    offset, timeout = args
    return parse(fetch_page(_session(), offset, timeout), offset)


# threads each waiting on its own requests
def fetch_threaded(pages: list[int], workers: int = 10, on_page=None) -> list[Medicine]:
    timeout = load().timeout
    out: list[Medicine] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for records in pool.map(_fetch_one_threaded, [(o, timeout) for o in pages]):
            if on_page:
                on_page(records)
            out.extend(records)
    return out


def _fetch_one_proc(args: tuple[int, int]) -> list[Medicine]:
    ## unit of work handed to a process
    offset, timeout = args
    with requests.Session() as session:
        return parse(fetch_page(session, offset, timeout), offset)


def fetch_multiproc(pages: list[int], workers: int = 10, on_page=None) -> list[Medicine]:
    timeout = load().timeout
    out: list[Medicine] = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        # fetching multiprocesses
        for records in pool.map(_fetch_one_proc, [(o, timeout) for o in pages]):
            if on_page:
                on_page(records)
            out.extend(records)
    return out


#  async event function
async def fetch_async(pages: list[int], workers: int = 10, on_page=None) -> list[Medicine]:
    config = load()
   #limit on how many requests are done at once
    sem = asyncio.Semaphore(workers)

    async def get_page(client: httpx.AsyncClient, offset: int) -> list[Medicine]:
        async with sem:
            return parse(await fetch_page_async(client, offset), offset)

    out: list[Medicine] = []
    # one client for the whole run
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(config.timeout),
        limits=httpx.Limits(max_connections=workers),
    ) as client:
        tasks = [asyncio.create_task(get_page(client, o)) for o in pages]
        # taken in order of pages requested, not order in which it returns
        for task in tasks:
            records = await task
            if on_page:
                on_page(records)
            out.extend(records)

    return out


# function lookup
MODELS = {
    "sequential": fetch_sequential,
    "threaded": fetch_threaded,
    "multiproc": fetch_multiproc,
    "asyncio": fetch_async,
}
