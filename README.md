# PharmaCLI Tool v3

A pharmacy CLI that reads CMS NADAC drug pricing CSVs, cleans them, and writes
them back out. It can also fetch NADAC records straight from the CMS API into
SQLite, using four different concurrency models so they can be compared.

## Install

```bash
git clone <repo> && cd pharmacli
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env      # optional, default already built in
```

Three dependencies: `httpx`, `requests`, `python-dotenv`.

## Settings

Read from `.env` or the environment. An environment variable beats the file.

| variable | default | what it does |
|---|---|---|
| `PHARMACLI_DB` | `pharmacli.db` | SQLite file to write to |
| `PHARMACLI_WORKERS` | `10` | pool size / requests in flight |
| `PHARMACLI_TIMEOUT` | `30` | per-request timeout in seconds |
| `PHARMACLI_LOG_LEVEL` | `INFO` | logging level |

## Commands


pharmacli clean   input.csv --rule NAME[:KEY=VALUE,...]
pharmacli dedup   input.csv --key row|ndc|ndc+date
pharmacli sort    input.csv --by description|ndc|price|date
pharmacli columns
pharmacli rules

pharmacli fetch  [--model sequential|threaded|multiproc|asyncio] [--workers N] [--limit N] [--db PATH] [--fresh]
pharmacli bench  [--workload io|cpu|both] [--workers N] [--limit N]


`-o FILE` writes output to a file, `-n` reports without writing, `-v` turns on
debug logging. Diagnostics go to stderr, so `-o` can be omitted and the CSV piped.

Exit codes: `0` ok, `1` no usable rows, `2` bad input, `3` bad rule, `4` fetch failed.



## Benchmark results

Measured on machine (8 cores), 2000 records in pages of 100, 10 workers:

**Workload A — fetching over the network**

| model | seconds | vs sequential |
|---|---:|---:|
| sequential | 1.65 | 1.00x |
| threaded | 0.39 | 4.23x |
| multiproc | 0.43 | 3.81x |
| asyncio | 0.36 | 4.57x |

**Workload B — parsing 60,000 rows already in memory**

| model | seconds | vs sequential |
|---|---:|---:|
| sequential | 1.74 | 1.00x |
| threaded | 6.19 | **0.28x** |
| multiproc | 0.63 | 2.75x |

## Known limitations

- **No tests.** Nothing here is covered by an automated test.
