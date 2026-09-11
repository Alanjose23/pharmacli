# PharmaCLI

A command-line tool for a pharmacy's drug costs. It downloads the weekly CMS
NADAC price file into SQLite, compares it against your inventory, and tells you
what you're losing money on, what to reorder, and when prices change next.

## Install

```bash
git clone <repo> && cd pharmacli
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env      # optional; the defaults work
```

The core install needs two packages, `httpx` and `python-dotenv`. The extras
add features:

| extra | adds |
|---|---|
| `.[analysis]` | `forecast` |
| `.[analysis,track]` | `sweep` (trains and promotes the model) |
| `.[nlp]` | search for a drug by describing it |
| `.[serve]` | the prediction API |

## Quick start

```bash
pharmacli fetch                                     # download ~1M weekly prices (several minutes)
pharmacli watch import data/pharmacy_inventory.csv  # load your shelf: ndc,cost[,on_hand,monthly]
pharmacli today                                     # the three worklists, ranked by money
pharmacli chat                                      # the same answers, in a browser
```

## Commands

| command | does |
|---|---|
| `fetch` | Downloads NADAC prices from CMS into the database |
| `watch` | Lists your shelf; `add`, `rm`, `import FILE.csv`, `packs` |
| `check FILE.csv` | Checks a CSV's drugs without adding them to your shelf |
| `today` | What to appeal, what to buy, and what's sitting dead |
| `margin` | Your cost against NADAC; `-o appeal.csv` writes a MAC appeal pack |
| `reorder` | What to order and when, in whole packs where the pack size is known |
| `lookup NDC` | One drug, from the panel and from openFDA; `--history` for its prices |
| `ask "question"` | 10 plain-English questions; `ask` alone lists them |
| `ask -o drugs.csv` | Every shelf drug's answers, one row each |
| `chat` | `ask` in a browser, on `127.0.0.1:8765` |
| `forecast` | Which drugs the model expects to move next week |
| `sweep` | Trains the model and promotes the winner |

[COMMANDS.md](COMMANDS.md) lists every command and the code each one runs.
[QUESTIONS.md](QUESTIONS.md) covers the 10 questions.
[DATABASES.md](DATABASES.md) explains every database and data folder.

## Settings

Read from `.env` or the environment. An environment variable overrides the file.

| variable | default | what it does |
|---|---|---|
| `PHARMACLI_DB` | `pharmacli.db` | the SQLite file |
| `PHARMACLI_DISPENSING_FEE` | `10.64` | state dispensing fee; sets the break-even in `margin` |
| `PHARMACLI_MODEL` | `models/pharmacli-mover.joblib` | the model `forecast` and the API load |
| `PHARMACLI_WORKERS` | `10` | downloads in flight during `fetch` |
| `PHARMACLI_TIMEOUT` | `30` | per-request timeout, in seconds |
| `PHARMACLI_LOG_LEVEL` | `INFO` | logging level |

`PHARMACLI_DB` is a relative path, so run commands from the project folder or
set it to an absolute path.

## Commands that delete data

- `fetch --fresh` deletes the database before downloading, without asking.
- `bench --workload io` deletes whatever file you pass as `--db`, four times per
  run.

## What it can't answer

- **A future price.** It gives the odds that a price moves and when; it never
  predicts a price.
- **Clinical questions.** It covers prices and inventory only, with no dosing,
  interactions or substitutions.
- **Whether an appeal will win.** NADAC is a survey of what pharmacies paid, and
  your PBM contract terms aren't in the data.
- **Anything after the newest NADAC file**, such as shortages or recalls.

Timing an order around a price change is worth cents per drug. The money is in
the totals across the whole shelf.

## Exit codes

`0` ok · `1` no usable rows · `2` bad input · `3` bad rule · `4` download failed
