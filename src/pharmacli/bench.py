# comparing models
# timing concurrency by waiting(fetching) and calculating(parsing)

import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

from pharmacli.collect import PAGE_SIZE, fetch_async, fetch_multiproc, fetch_sequential, fetch_threaded, to_row
from pharmacli.models.medicine import Medicine

log = logging.getLogger(__name__)

# small enough to finish while someone is watching
CPU_ROWS = 60000
# how many rows the benchmark parses(CPU)


# fake api record for CPU work
# measures parsing speed and waits on API
def sample_record(i: int) -> dict:
    return {
        "ndc_description": f"DRUG {i} 10 MG TABLET",
        #####
        "ndc": f"{i:011d}",
        #####
        "nadac_per_unit": "0.02489",
        "effective_date": "2026-01-01",
        "pricing_unit": "EA",
        "pharmacy_type_indicator": "C/I",
        "otc": "N",
        "explanation_code": "1",
        "classification_for_rate_setting": "G",
        "corresponding_generic_drug_nadac_per_unit": "",
        "corresponding_generic_drug_effective_date": None,
        "as_of_date": "2026-02-01",
    }


def parse_chunk(rows: list[dict]) -> int:
    ## parses chunk of rows and reports if medicine is valid or not
    return sum(1 for r in rows if isinstance(Medicine.arow(to_row(r), 0), Medicine))


def chunks(rows: list[dict], n: int) -> list[list[dict]]:
    # splitting list into pieces for workers
    size = max(1, len(rows) // n)
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def timed(fn, *args):
    # runs function reports time, emphasizing time elapsed for a function
    start = time.perf_counter()
    result = fn(*args)
    return time.perf_counter() - start, result



### fetching 
# times each of the four models fetching exactly the same pages
def bench_io(limit: int, workers: int, db: str) -> list[tuple]:
    import asyncio

    from pharmacli.store import connect, insert

    pages = list(range(0, limit, PAGE_SIZE))
    results = []

    for name, fn in (
        ("sequential", fetch_sequential),
        ("threaded", fetch_threaded),
        ("multiproc", fetch_multiproc),
        ("asyncio", None),
    ):
        # having a fresh database for every run
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db + suffix)
            except FileNotFoundError:
                pass

        # timing run, records collected and writteno
        try:
            if name == "asyncio":
                elapsed, records = timed(lambda: asyncio.run(fetch_async(pages, workers)))
            else:
                elapsed, records = timed(fn, pages, workers)
        except Exception as exc:
            # if failed resume other models
            log.error("%s failed: %s", name, exc)
            results.append((name, None, 0, 0.0))
            continue

        conn = connect(db)
        insert(conn, records)
        conn.close()
        results.append((name, elapsed, len(records), len(records) / elapsed if elapsed else 0))

    return results



## parsing rows, no async row here, only sequential, threaded. and multiproc
def bench_cpu(workers: int) -> list[tuple]:
    rows = [sample_record(i) for i in range(CPU_ROWS)]
    pieces = chunks(rows, workers)
    results = []

    # baseline, just parsing the lot
    elapsed, _ = timed(parse_chunk, rows)
    results.append(("sequential", elapsed, CPU_ROWS, CPU_ROWS / elapsed if elapsed else 0))

    def run_threads():
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return sum(pool.map(parse_chunk, pieces))

    elapsed, _ = timed(run_threads)
    results.append(("threaded", elapsed, CPU_ROWS, CPU_ROWS / elapsed if elapsed else 0))

    def run_procs():
        with ProcessPoolExecutor(max_workers=workers) as pool:
            return sum(pool.map(parse_chunk, pieces))

    elapsed, _ = timed(run_procs)
    results.append(("multiproc", elapsed, CPU_ROWS, CPU_ROWS / elapsed if elapsed else 0))

    return results


# results table, records time and which process to run
def show(title: str, rows: list[tuple], baseline_name: str = "sequential") -> None:
    baseline = next((r[1] for r in rows if r[0] == baseline_name and r[1]), None)
    print(f"\n{title}")
    print(f"{'model':<12} {'seconds':>9} {'records':>9} {'rec/s':>10} {'vs seq':>8}")
    print("-" * 52)
    for name, elapsed, records, rate in rows:
        if elapsed is None:
            print(f"{name:<12} {'FAILED':>9}")
            continue
        speedup = f"{baseline / elapsed:.2f}x" if baseline and elapsed else "-"
        print(f"{name:<12} {elapsed:>9.2f} {records:>9} {rate:>10.0f} {speedup:>8}")


# runs version and gets tables
def run(workload: str, limit: int, workers: int, db: str) -> None:
    
    print(f"cpu cores: {os.cpu_count()}    workers: {workers}")
    # prints core count to maintain readability on all machines

    if workload in ("io", "both"):
        print(f"\nfetching {limit} records in pages of {PAGE_SIZE}"
              f" ({len(range(0, limit, PAGE_SIZE))} requests per model)")
        show("workload A - network fetch", bench_io(limit, workers, db))

    if workload in ("cpu", "both"):
        print(f"\nparsing {CPU_ROWS} rows already in memory")
        show("workload B - parsing (CPU bound)", bench_cpu(workers))
