# PharmaCLI Tool v3

A pharmacy CLI that reads CMS NADAC drug pricing CSVs, cleans them, and writes
them back out. It can also fetch NADAC records straight from the CMS API into
SQLite, using this as a baseline for comparing with future prices. Future functionality includes creating an order button so that the code would be able to run on pharmacy data and would let the pharmacy predict future prices and order drugs accordingly.

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

### Data manipulation

pharmacli clean   input.csv --rule NAME[:KEY=VALUE,...]


pharmacli dedup   input.csv --key row|ndc|ndc+date


pharmacli sort    input.csv --by description|ndc|price|date

### List medicine

pharmacli columns


pharmacli rules

### API commands

pharmacli fetch  [--model sequential|threaded|multiproc|asyncio] [--workers N] [--limit N] [--db PATH] [--fresh] [--page-size N]


pharmacli bench  [--workload io|cpu|numpy|both] [--workers N] [--limit N] [--db PATH]


`-o FILE` writes output to a file, 

`-n` reports without writing,

 `-v` turns on
debug logging. 

Diagnostics go to stderr, so `-o` can be omitted and the CSV piped.

## Exit Codes

Exit codes: `0` ok, `1` no usable rows, `2` bad input, `3` bad rule, `4` fetch failed.

### Two commands delete data

- `fetch --fresh` deletes the database before fetching, with no confirmation.
- `bench --workload io` deletes its database four times per run, once per model,
  so each is timed from empty. It defaults to a scratch file in the temp
  directory — but **anything you pass as `--db` will be deleted.**

`bench --workload cpu` and `--workload numpy` touch no files.

### Page size

`fetch --page-size` defaults to 100.

`--page-size` is passed to workers through a module global, so it is only safe
with `--model multiproc` on fork platforms (Linux). The CLI refuses that
combination elsewhere rather than silently fetching partial data.



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

- One week a month, 93% of every drug reprices at once, every other week, nothing moves. CMS monthly file establishes this and publishes data in advance
- `pharmacli lookup` matches roughly 17 in 20 NDCs., because nadac api stores discontinued medicines as well..


## Analysis

The EDA layer is an optional extra, because the CLI itself never imports pandas.

```bash
pip install -e ".[analysis]"
pharmacli fetch --model asyncio --page-size 5000 --limit 1030000 --fresh
jupyter lab notebooks/eda.ipynb
```


## Vectorization

Week-over-week percent change over 1,000,000 prices (`pharmacli bench --workload numpy`):

| approach | seconds | vs python loop |
|---|---:|---:|
| python loop | 0.165 | 1.00x |
| pandas | 0.009 | 18.6x |
| numpy | 0.003 | **50.7x** |


