# PharmaCLI

A command-line tool for a pharmacy's drug costs, reimbursement and label questions.

It downloads the CMS NADAC (National Average Drug Acquisition Cost) weekly price
files into a SQLite database. It then compares those prices with your own
inventory CSV and tells you:

- **what you are losing money on**: drugs whose cost to you is above NADAC, in dollars a month, with a CSV to support a MAC appeal
- **what to reorder and when**: days of supply left, the order-by date, and whole packs to order
- **what capital is sitting dead**: stock with more days of supply than you set as the limit, and drugs CMS no longer prices
- **when prices change next**: CMS reprices on a published monthly calendar
- **which claims were underpaid**: claims paid below NADAC × quantity + dispensing fee, or below your own cost
- **which brands have a cheaper generic**: using the generic price NADAC already carries on brand rows
- **which FDA recalls touch your shelf**
- **what a package insert or a reimbursement rule says**, cited to the page. It refuses questions about a specific patient and questions its documents don't cover.

It also contains the course exercises it was built through: a CSV cleaner, a
concurrency benchmark, a repricing model with an API, neural-network and NLP
experiments.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                 # core: one dependency, httpx
cp .env.example .env             # optional; the defaults work
```

Each extra adds features:

| extra | needed for |
|---|---|
| *(core)* | `fetch`, `watch`, `check`, `today`, `margin`, `reorder`, `lookup`, `ask` (the 14 questions; find-a-drug matches words in the drug name), `claims`, `generics`, `recalls`, `clean`, `bench --workload io/cpu` |
| `.[analysis]` | `forecast`, `nlp --task baseline`, `bench --workload numpy` |
| `.[analysis,track]` | `sweep` (MLflow). `dvc` is a separate install (`pipx install dvc`), used by hand to version the database |
| `.[serve]` | `chat`, and the API's document and health endpoints |
| `.[analysis,serve]` | the API's `/predict` as well (`.[analysis,serve,rag]` for the document endpoints). `docker compose up` installs all three itself |
| `.[analysis,track,dl]` | `nn`. Install torch from the CPU index first: `pip install --index-url https://download.pytorch.org/whl/cpu torch` |
| `.[analysis,nlp]` | `nlp --task finetune/search`, and matching by meaning in `ask "find a drug by description"` ("cholesterol pill" → Lipitor) |
| `.[rag]` | `docs`, `interactions`, and document answers in `ask`. Also needs Chroma and Ollama (see below) |
| `.[notebook]` | the two notebooks in `notebooks/` — matplotlib, JupyterLab and shap. No module in `src/` imports them |

## Quick start

```bash
pharmacli fetch --limit 2000000 --page-size 5000   # every weekly price row in the CMS file (1.1M and growing)
pharmacli watch import my_inventory.csv            # load your shelf (format below)
pharmacli today                                    # appeal / buy / clear, ranked by money
pharmacli chat                                     # the same questions in a browser
```

`fetch` with no flags takes only 5,000 rows, which is enough to try it. An
interrupted fetch picks up where it stopped when you run it again.

A fresh clone has no data: `.gitignore` excludes `*.csv`, `pharmacli.db`, `models/`,
`data/docs/` and `*.npz`. The sample files named below
(`data/pharmacy_inventory.csv`, `data/pharmacy_broken.csv`, …) exist only on the
machine that made them.

Run commands from the project folder: the database, `corpus.json`, `data/` and
`models/` are relative paths.

## The CSV files

The tool reads four kinds of CSV. Your shelf CSV is the one that matters: the
database stores it and every money command reads from it.

### 1. Inventory: your shelf (`watch import FILE`, `check FILE`)

Columns are read **by position**. A header row is optional, and extra columns
after the fourth are ignored.

| position | column | required | meaning |
|---|---|---|---|
| 1 | `ndc` | yes | 11-digit NDC, digits only (no hyphens). Shorter values get zeros added on the left |
| 2 | `cost` | yes | what you pay **per NADAC pricing unit** (per EA, ML or GM, whichever NADAC uses for that drug) |
| 3 | `on_hand` | no | units in stock now |
| 4 | `monthly` | no | units dispensed per month |

```csv
ndc,cost,on_hand,monthly,description
16714096301,0.81031,102,78,ABIRATERONE ACETATE 250 MG TAB
```

- A line is skipped, with the reason logged, when the NDC isn't digits, the cost
  is missing, or a number is negative or not a number.
- Without `on_hand` and `monthly` you still get the per-unit gap and the break-even,
  but no dollar totals, reorder dates or dead stock.
- Importing again updates every NDC in the file. The cost is always replaced;
  a blank `on_hand` or `monthly` keeps the value already stored. NDCs not in the
  file stay on the shelf (`watch rm NDC` removes them).
- `check FILE` runs `today`'s analysis on the file without saving it (its defaults
  are `--within 14 --dead-days 90`, not `today`'s 21 and 180).

### 2. NADAC price file (`clean`)

The CMS NADAC download format. The header must be exactly these 12 columns, in
this order (`pharmacli columns` prints them):

`NDC Description, NDC, NADAC Per Unit, Effective Date, Pricing Unit, Pharmacy Type Indicator, OTC, Explanation Code, Classification for Rate Setting, Corresponding Generic Drug NADAC Per Unit, Corresponding Generic Drug Effective Date, As of Date`

- Dates may be `MM/DD/YYYY`, `YYYY-MM-DD` or `MM/DD/YY`. OTC may be `Y/N`, `yes/no`, `true/false`, `1/0`.
- A row is skipped, with its line number and reason, when it: is blank, has the
  wrong number of fields, contains bytes that aren't valid UTF-8, repeats the header,
  lacks an NDC or description, or has a missing, malformed or non-positive price.
  A bad *generic* price only drops the generic price, not the row, and an
  unreadable date or OTC value is written back blank.
- Output has the same 12 columns, with dates as `YYYY-MM-DD` and OTC as `Y`/`N`.
- Rules: `dedup` keeps the first row per `row` (identical rows), `ndc`, or
  `ndc+date` (NDC + effective date). `sort` orders by `description`, `ndc`,
  `price` or `date` (effective date; rows without one go last).

`data/pharmacy_broken.csv` is a 5,230-row sample with deliberately broken rows.

```bash
pharmacli clean data/pharmacy_broken.csv --rule dedup:key=ndc --rule sort:by=price -o clean.csv
pharmacli clean data/pharmacy_broken.csv --rule dedup:key=ndc+date --dryrun
```

### 3. Claims (`claims check FILE`)

Columns are read **by header name**.

| column | required | meaning |
|---|---|---|
| `date` | yes | fill date, `YYYY-MM-DD` |
| `ndc` | yes | 11 digits |
| `qty` | yes | units dispensed (> 0), in the NADAC pricing unit |
| `paid` | yes | total dollars the payer paid for the claim (≥ 0) |
| `rx` | no | prescription number, carried through to the output |
| `payer` | no | payer name; matched against the payers CSV |

For each claim, the benchmark is (the NADAC price in effect on the fill date) × `qty`
+ the payer's dispensing fee. The price in effect is the newest weekly file on or
before that date; a fill older than the first file uses the oldest price. Claims
for an NDC with no NADAC price are counted as "unpriced" and not checked.
- **underpaid** = `benchmark − paid` when positive
- **below cost** = `your cost × qty − (paid − fee)` when positive, if your shelf has a cost for that NDC

### 4. Payers (`--payers FILE`)

| column | meaning |
|---|---|
| `payer` | must match the `payer` values in the claims file exactly |
| `dispensing_fee` | dollars per claim |

A payer missing from the file, or no `--payers` at all, gets `PHARMACLI_DISPENSING_FEE` (10.64).

`pharmacli claims sample` writes a **synthetic** claims file and a payers file with
illustrative fees (`data/claims_sample.csv`, `data/payers.csv`). It builds them from
shelf drugs that have a monthly volume, with a fixed seed, and caps them at 2,000
claims. Pass the payers file when you check, or every claim is measured against
the $10.64 default fee:

```bash
pharmacli claims sample
pharmacli claims check data/claims_sample.csv --payers data/payers.csv -o underpaid.csv
```

Replace both files with a real claims export and your contract fees.

### CSVs it writes

| command | columns |
|---|---|
| `margin -o FILE` | `ndc, description, nadac_file, nadac_unit, your_cost, gap_per_unit, breakeven_qty, monthly_units, monthly_loss`. Only lines costing more than NADAC: the MAC appeal pack |
| `claims check … -o FILE` | `date, rx, ndc, payer, qty, paid, nadac_unit, fee, benchmark, shortfall, cost_unit, below_cost_loss`. Only underpaid claims, largest shortfall first |
| `ask -o FILE` | one row per shelf drug: `ndc, description, nadac_file, nadac_unit, pricing_unit, rate_class, otc, effective_date, weeks_priced, weeks_changed, first_price, price_change_pct, price_age_days, move_odds, days_to_likely_move, expected_price, on_hand, monthly, days_of_supply, reorder_action, reorder_why, order_qty, timing_value, your_cost, breakeven` |
| `clean -o FILE` | the 12 NADAC columns |

## Commands

### Prices and inventory

| command | does |
|---|---|
| `fetch` | Downloads NADAC rows from the CMS API (data.medicaid.gov) into the `nadac` table. `--limit N` rows, `--page-size` up to 5000, `--fresh` deletes the database first |
| `watch` | Lists your shelf and flags NDCs missing from the newest NADAC file. `watch add NDC… [--cost X]`, `watch rm NDC…`, `watch import FILE.csv` |
| `watch packs` | Looks up pack sizes on openFDA for up to `--limit` (200) shelf NDCs, the ones with the fewest days of supply first, and caches them so `reorder` orders whole packs. openFDA allows 1,000 lookups a day without a key; run it again to continue |
| `check FILE.csv` | `today`'s analysis on a CSV, without adding it to your shelf (`--within 14`, `--dead-days 90`). Also lists NDCs with no price in the newest file |
| `today` | Three worklists. **APPEAL**: drugs costing more than NADAC, in $/month. **BUY**: orders due within `--within` (21) days. **CLEAR**: over `--dead-days` (180) days of supply, and stocked NDCs CMS stopped pricing. Also shows the next repricing date |
| `margin` | Every shelf drug: your cost, NADAC, gap per unit, break-even quantity (the units per script at which the dispensing fee stops covering the gap), $/month. `--top N`; `-o FILE` writes the appeal pack |
| `reorder` | Order-by date (days of supply − `--lead-days` 2), quantity (one month, rounded up to whole packs when known), and whether to order early or wait for a likely price change. `--within 14`, `--as-of YYYY-MM-DD`, `--top 40` |
| `lookup NDC` | Drug name, form, route, labeler and pack from openFDA, plus its newest NADAC price. `--history` adds every weekly price and a sparkline |
| `ask "question"` | The 12 questions below, in plain English. `ask` alone lists them; `--ndc` / `--phrase` supply the drug |
| `ask -o FILE.csv` | Every shelf drug's answers, one row each |
| `chat` | `ask` in a browser at `127.0.0.1:8765` (`--port`, `--no-open`). Attach an inventory CSV to ask about that file instead of your saved shelf, then save it to the shelf if you want to keep it. Runs the API on localhost and opens the page with a token in the URL fragment; `--no-open` prints that URL to copy. Needs `.[serve]` |

The 14 `ask` questions: what am I losing money on · what should I order this week ·
what capital is sitting dead · when do prices change next · what is on my shelf ·
what is this drug · what has this drug's price done · will this drug's price move ·
when should I reorder this drug · find a drug by description · which stocked drugs
are recalled · which brands have cheaper generics · which stocked drugs are missing a price ·
what changed since my saved shelf.

The last one needs a CSV attached in `chat`; the rest answer from your saved shelf,
or from an attached CSV when there is one.

Where the price rules come from: CMS publishes the NADAC monthly file on the first
Monday on or after the 15th, and the weekly file dated that Wednesday carries it.
The odds that a price moves next week depend on how old the price is, peaking at
21–28 days. The expected size of a move depends on the rate class (generics drift
down; brands are flat). These are rates measured on the panel, stored as constants
in `cli.py`.

### Reimbursement and safety

| command | does |
|---|---|
| `claims sample` | Writes synthetic claims + illustrative payer fees from your shelf (`-o`, `--payers`, `--days 30`, `--seed 7`) |
| `claims check FILE.csv` | Claims paid below NADAC + fee and below your cost, totals by payer, `-o` CSV, and the `docs appeal` command for the NDC with the largest shortfall |
| `generics` | Brand shelf drugs (`B`, `B-ANDA`) whose NADAC row carries a cheaper generic price, in $/month. A purchasing conversation, not a substitution |
| `recalls` | FDA drug recalls reported in the last `--days` (90) that match a shelf NDC. A match on the labeler and product code counts every pack size. Recalls are lot-specific: check the lots |
| `interactions DRUG DRUG…` | For 2–6 drugs, asks the documents about every pair and prints the answer with its label citations. Drugs with no label are listed as not checked. An answer with no interaction does not mean the drugs are safe together |

### Documents: package inserts and reimbursement rules

Needs `pip install -e ".[rag]"` and the two services in `docker-compose.yml`:

```bash
docker compose up -d --wait chroma ollama   # first start pulls the images and both models (several GB)
pharmacli docs fetch                 # downloads the 56 PDFs listed in corpus.json
pharmacli docs ingest                # parses, chunks and indexes them; skips if already indexed
pharmacli ask "what are the boxed warnings on carbamazepine"
pharmacli docs handout gabapentin
pharmacli interactions carbamazepine lamotrigine
pharmacli docs appeal 65162069890 -o appeal.md
```

The corpus is 50 FDA package inserts (the highest-spend drugs on the sample shelf)
and 6 reimbursement documents: the CMS NADAC methodology, the CMS NADAC Q&A,
42 CFR 447 Subpart I, NY Public Health Law §280-a, and the eMedNY pharmacy policy
and billing guidelines. The list is in `corpus.json`; rebuild it with
`docs fetch --from-shelf N`. For a drug with no label, `docs handout` and
`interactions` say "no label in the corpus". `ask` refuses an NDC with no label,
but a drug *name* with no label is searched across every document and can be
answered from another drug's label if a passage clears the relevance floor.
Check the title in each citation.

| command | does |
|---|---|
| `docs fetch` | Downloads the PDFs in `corpus.json` to `data/docs/`. `--from-shelf N` rebuilds `corpus.json` from your N highest-spend shelf drugs (one label per generic, via openFDA and DailyMed) |
| `docs ingest` | Extracts text (PyMuPDF) and splits it two ways: `fixed` (800-character chunks, 150 overlap) and `section` (split at label/statute headings). Embeds the chunks with Ollama and stores them in Chroma, one collection per way of splitting |
| `docs refresh` | Re-downloads labels that have a newer version and reimbursement documents whose checksum changed, re-indexes only those, and prints what changed in each label (boxed warnings first). The old PDF is kept as `<doc_id>.prev.pdf` |
| `docs eval` | Scores `tests/rag_qa.jsonl` (50 questions). `--retrieval-only` in seconds; the full run also generates answers (minutes to hours). `--json FILE` saves a run; `--compare A.json B.json` prints the before/after |
| `docs appeal NDC` | A MAC appeal draft in Markdown: a table of facts from your panel and shelf, an argument paragraph citing the reimbursement documents, and a warning for any number that appears in neither. The NDC must be on your shelf with a cost above NADAC |
| `docs handout DRUG` | The label's section 17 (Patient Counseling Information), word for word, with pages |

How a question is answered:

1. **Routing.** `ask` sends a question to the documents when it has a label word
   (dose, contraindication, interaction, warning, pregnancy, overdose …), when it
   matches no price question, or when it has a reimbursement word (MAC, appeal,
   CMS, Medicaid …) and only a weak price match.
2. **Refusal before retrieval.** It refuses patient-specific questions ("should I give
   my 80-year-old patient…", "my patient has a CrCl of 25…") and questions naming an
   NDC that has no label in the corpus.
3. **Retrieval.** The top `PHARMACLI_K` (3) chunks are retrieved, limited to the drug's own label when the question names one.
4. **Refusal after retrieval.** It refuses when the best chunk's similarity is below
   `PHARMACLI_RELEVANCE_FLOOR` (0.35), or when the model says the context doesn't cover it.
5. **Answer.** The model answers only from the numbered chunks and cites each claim as
   `[n]` → document · section · page · label date. An answer that cites none of
   its chunks is not shown.

On a laptop CPU an answer takes about a minute, and a pair in `interactions` about 75 s.

Measured with `docs eval`, `section` chunking, k=3:
- the gold page is retrieved for 36 of 40 answerable questions (33 with `fixed`)
- all 5 questions that should be refused are refused
- 8 of the 40 answerable questions are wrongly refused
- 31 of 40 answers cite their chunks; fact recall is 0.61 and faithfulness 0.64

The answer key has not been reviewed yet (`"reviewed": false`), so treat these
scores as provisional.

### Model, API and exercises

| command | does |
|---|---|
| `forecast` | For each shelf NDC, the probability that its price moves ≥ 5% in next week's file, from the promoted model. `--min-prob 0.25`. Without a promoted model, it refits one (~40 s) |
| `sweep` | Trains 3 baselines and 11 models (HistGradientBoosting grid + logistic regression). The data is split by date: train before 2026-05-13, pick the winner on validation (to 2026-07-01), score test once. Logs to `mlflow.db`, registers `pharmacli-mover@champion`, exports `models/pharmacli-mover.joblib` + `.json`. `--sample N` trains on N NDCs and promotes nothing |
| `uvicorn pharmacli.serve:app` | The prediction API (see **The service** below for the container, the token and the document endpoints). `GET /health`; `POST /predict {"ndc": "00002353301", "overrides": {"unit_price": 250, "age_days": 25}}` (`overrides` optional). Returns the probability and `buy ahead`/`hold`. Won't start without a promoted model from `sweep`. Refuses an NDC whose features fall outside the training range (about 2%: prices unchanged for more than 20 weeks) |
| `nn --task toy` | Hand-derived backprop in numpy checked against PyTorch autograd |
| `nn --task tabular` | A feed-forward net on the same panel features (`--sample`, `--epochs`), logged to MLflow and `runs/tabular` |
| `nn --task xray` | A CNN on PneumoniaMNIST (downloaded to `data/`). An exercise, not a clinical tool |
| `nlp --task baseline` | Brand vs generic from the drug name: TF-IDF word vs character n-grams + logistic regression |
| `nlp --task finetune` | DistilBERT fine-tuned on the same split (`--epochs`) |
| `nlp --task search` | MiniLM semantic search against keyword search over 6,359 drug descriptions (`--query`) |
| `bench --workload io/cpu/numpy/both` | Times sequential, threaded, multiprocess and asyncio downloads; parsing on each; a Python loop vs pandas vs numpy |

### The service

The API, Chroma and Ollama as one stack. From a clean checkout:

```bash
echo "PHARMACLI_JWT_SECRET=$(openssl rand -hex 32)" >> .env  # compose will not start without it
docker compose up -d --wait                    # api on 127.0.0.1:8000; first build ~2 min
docker compose exec api pharmacli docs fetch   # 56 PDFs into ./data/docs   (first run only)
docker compose exec api pharmacli docs ingest  # chunk, embed, index        (first run only)
docker compose exec api pharmacli token        # paste into Swagger's Authorize box
```

Then open <http://127.0.0.1:8000/docs>. The image carries the package and
`corpus.json` and nothing else: the PDFs live in the mounted `./data`, the
models in the Ollama volume, and the token secret arrives as an environment
variable, so no data and no secret is baked into a layer.

| endpoint | auth | does |
|---|---|---|
| `GET /health` | open | liveness, uptime, the model it is serving (or why it is not), and whether the documents are reachable. The compose healthcheck calls it |
| `GET /chat` | open | the browser page `pharmacli chat` opens. A shell with no data in it; everything it fetches needs the token |
| `/chat/prompts`, `/chat/ask`, `/chat/upload`, `/chat/save`, `/chat/clear` | bearer | what that page calls: the question list, an answer, and the attached-CSV slot |
| `GET /metrics` | open | request counts, latency percentiles per route, token counts, and the median of each RAG stage — the numbers the latency report is built from |
| `POST /predict` | bearer | the week 6 prediction, answered from the committed demo fixture unless you mount the real panel and model |
| `POST /ask` | bearer | the whole answer as one JSON object: text, sources, refusal reason, per-stage timings, token counts |
| `POST /ask/stream` | bearer | the same answer as server-sent events, a token at a time |
| `pharmacli token` | — | mints an HS256 bearer token (`--sub`, `--ttl`). Must run with the same `PHARMACLI_JWT_SECRET` as the API |

```bash
TOKEN=$(docker compose exec -T api pharmacli token | tr -d '\r')
curl -N -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"question":"what are the boxed warnings on carbamazepine"}' \
     http://127.0.0.1:8000/ask/stream
```

**The demo fixture.** `/predict` needs a panel and a promoted model, and both are
gitignored — so a clean clone used to answer 503. Committed instead: `data/demo.db`
(300 NDCs sampled from the panel) and `models/demo-mover.joblib` trained on it,
2.1MB together, built by `python tests/make_demo.py --from pharmacli.db`. The model
is named `pharmacli-mover-demo`, so `/health` and every prediction say what they
are. For predictions that mean something, mount the real panel and model — the two
lines are in `docker-compose.yml`, commented, next to the `./data` one — and point
`PHARMACLI_DB` and `PHARMACLI_MODEL` at them.

Regenerate the fixture when the feature list changes; `tests/test_demo.py` fails
when it goes stale instead of letting the demo 503 in front of someone.

**What it logs.** One JSON line per request (`ts`, `level`, `request_id`,
`method`, `path`, `status`, `ms`), and the same `request_id` on the response as
`X-Request-ID` and in the body of every error. Send your own `X-Request-ID` and
it is kept, so a trace survives a proxy. `docker compose logs -f api`.

**Load test.** `tests/loadtest.py` streams the 50 evaluation questions at the
service at several concurrency levels and writes a latency report:

```bash
python tests/loadtest.py --n 12 --concurrency 1,2,4 --out latency.md
```

Each answer carries its own stage timings back from the server, so the report
names the bottleneck from measurements rather than guesswork: see
[latency.md](latency.md).

### CSV cleaning

| command | does |
|---|---|
| `clean FILE --rule NAME[:key=value,…]` | Parses a NADAC-format CSV and applies rules in order (`--rule` repeats). `-o FILE`, `-n/--dryrun` |
| `rules` / `columns` | Lists the rules / the 12 expected columns |

## Where the data lives

| path | what it holds | written by |
|---|---|---|
| `pharmacli.db` | SQLite. `nadac`: one row per NDC × effective date × weekly file (price, pricing unit, rate class, OTC, generic price, explanation code). `watch`: your shelf (ndc, cost, on_hand, monthly). `package`: openFDA pack sizes | `fetch`, `watch`, `watch packs` |
| `pharmacli.db.dvc` | DVC pointer to a snapshot of the database; `dvc checkout` restores it from the local DVC cache (no remote is configured). DVC is not a project dependency: install it yourself to use these files | `dvc add` |
| `corpus.json` | The document list: label set IDs, versions, label dates and NDCs; reimbursement document URLs and SHA-256 | `docs fetch --from-shelf`, `docs refresh` |
| `data/docs/` | The downloaded PDFs | `docs fetch`, `docs refresh` |
| `data/qa-src/` | Page-by-page text of each PDF, used to write the answer key. No command reads it | — |
| `tests/rag_qa.jsonl` | The answer key: question, expected lane, gold document and page, facts | by hand |
| Chroma volume `chroma-data` | The `chunks_fixed` and `chunks_section` collections | `docs ingest`, `docs refresh` |
| `models/pharmacli-mover.joblib` + `.json` | The promoted model and its card: metrics, threshold, feature ranges, data checksum | `sweep` |
| `mlflow.db`, `mlruns/` | MLflow runs and model registry | `sweep`, `nn --task tabular` |
| `runs/` | TensorBoard logs | `nn` |
| `data/desc-embeddings.npz` | Cached MiniLM vectors for the drug descriptions | `nlp --task search`, `ask "find a drug…"` |
| `data/pneumoniamnist.npz` | MedMNIST dataset | `nn --task xray` |
| `data/*.csv`, `drugs.csv` | Sample inventory, broken NADAC file, synthetic claims and payers; an `ask -o` output | see [The CSV files](#the-csv-files) |
| `notebooks/` | Exploratory analysis of the price panel (`eda.ipynb`) and the week 5 model (`week5.ipynb`; its explainability cell needs `pip install shap`) | — |

## What each source file does

All code is in `src/pharmacli/`.

| file | role in the data flow |
|---|---|
| `cli.py` | Every command's handler, the dispatch table and `main` |
| `parser.py` | Every command's arguments: one `argparse` tree, and the `--version` string |
| `desk.py` | The price maths: repricing calendar, move odds by price age, expected move by rate class, reorder advice, break-even, and the `today`/`check` worklists. Imports nothing from `cli` |
| `config.py` | Reads the `PHARMACLI_*` settings from `.env` and the environment |
| `err.py` | Error types (bad input, bad rule, failed download) |
| `output.py` | Writes to a file or stdout |
| `reader.py` | Reads a NADAC-format CSV: checks the header, parses each row, collects skipped rows with reasons |
| `models/rows.py` | The 12 column names and the skipped-row record |
| `models/medicine.py` | One row → a `Medicine` (price as `Decimal`, dates, OTC, generic price) and back to a CSV row |
| `rules/dedup.py`, `rules/sort.py`, `rules/__init__.py` | The two cleaning rules and the lookup that builds them by name |
| `protocols.py` | The rule interface; `dispatch` in `cli.py` runs the rules in order and counts what each removed |
| `collect.py` | Downloads NADAC pages from the CMS API with four concurrency models; retries 429/5xx; maps API fields to the 12 columns |
| `store.py` | The SQLite schema and every read and write: insert prices (duplicates ignored), upsert the shelf, cache packs, newest week, last-priced week |
| `enrich.py` | openFDA NDC Directory lookup (name, form, route, labeler, pack size) and an NDC's price history |
| `bench.py` | The `bench` timings |
| `frame.py` | Loads the `nadac` table into pandas; week-over-week % change; per-class move profile |
| `model.py` | Features (log price, moved last week, moves in 8 weeks, 4-week volatility, weeks since move, price age, day of month), target (moves ≥ 5% next week), time split, pipeline, baselines, loading the promoted model |
| `track.py` | The MLflow sweep and model promotion |
| `serve.py` | The FastAPI service: `/predict`, `/ask`, `/ask/stream`, the `/chat/*` page endpoints, JWT, JSON request logging and `/metrics` |
| `nn.py` | The `nn` tasks |
| `nlp.py` | The `nlp` tasks; keyword search and the embedding cache used by `ask` |
| `ask.py` | The question list, routing between price and document answers, and the `ask -o` CSV |
| `chat.html` | The page `chat` serves. Its endpoints are `/chat/*` on the FastAPI app |
| `claims.py` | Synthetic claims, the underpayment check, the brand → generic gaps |
| `docs.py` | `corpus.json`, PDF downloads (DailyMed, CMS, govinfo, NY), openFDA label lookup, refresh |
| `rag.py` | PDF parsing, both chunkers, Chroma and Ollama over HTTP, retrieval, refusals, citations, appeal drafting |
| `rageval.py` | Scores the answer key and compares two runs |
| `safety.py` | Recall matching, label change digest, counseling handout, interactions |

`tests/` holds the pytest suite: `pip install -e ".[analysis,serve,rag]"` then `pytest -q`.
`tests/loadtest.py` and `tests/make_demo.py` are not part of it — the first needs the
service running (see **The service**), the second regenerates the committed demo fixture.

Lint with `ruff check src tests` (`pip install ruff`; the rules are in `pyproject.toml`)
and types with `mypy src/pharmacli`. CI runs the suite on 3.10/3.11/3.12, lints once,
and builds the image and curls its `/health` to prove the container still starts.

Outside `src/`: `Dockerfile` (two stages, the runtime copies only the built venv),
`docker-compose.yml` (api + Chroma + Ollama) and `.dockerignore` (without it the build
context is 1.5GB of data that must not reach the image).

## Settings

Read from `.env` or the environment; an environment variable overrides the file.

| variable | default | what it does |
|---|---|---|
| `PHARMACLI_DB` | `pharmacli.db` | the SQLite file |
| `PHARMACLI_DISPENSING_FEE` | `10.64` | dispensing fee for break-even, appeals, and payers not in `--payers` |
| `PHARMACLI_MODEL` | `models/pharmacli-mover.joblib` | the model `forecast` and the API load |
| `PHARMACLI_WORKERS` | `10` | concurrent downloads in `fetch` |
| `PHARMACLI_TIMEOUT` | `30` | per-request timeout, in seconds |
| `PHARMACLI_LOG_LEVEL` | `INFO` | logging level (`-v` for debug) |
| `PHARMACLI_CHROMA_URL` | `http://127.0.0.1:8001` | Chroma server |
| `PHARMACLI_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama server |
| `PHARMACLI_LLM` | `qwen2.5:3b-instruct` | answering model; `qwen2.5:1.5b-instruct` needs half the memory |
| `PHARMACLI_EMBED` | `all-minilm:l6-v2` | embedding model for documents and questions |
| `PHARMACLI_CHUNKING` | `section` | `fixed` or `section`: which collection answers |
| `PHARMACLI_K` | `3` | chunks retrieved per question |
| `PHARMACLI_RELEVANCE_FLOOR` | `0.35` | below this similarity a question is refused |
| `PHARMACLI_JWT_SECRET` | *(none)* | signs the API's bearer tokens. Unset, the API generates a per-process one and logs a warning: the endpoints stay closed, but `pharmacli token` in another shell will not verify |

## Commands that delete or overwrite data

- `fetch --fresh` deletes the database before downloading, without asking.
- `bench` with `--workload io` or the default `both` deletes the `--db` file four
  times per run (default: a file in the temp folder). Never point `--db` at `pharmacli.db`.
- `watch import` replaces the stored cost for every NDC in the file.
- `claims sample` overwrites `data/claims_sample.csv` and `data/payers.csv` (or the `-o`/`--payers` paths).
- `docs fetch --from-shelf` overwrites `corpus.json`; `docs refresh` rewrites it and replaces PDFs.
- `sweep` without `--sample` replaces `models/pharmacli-mover.joblib` and `.json`.

## What it can't answer

- **A future price.** It gives the odds that a price moves and when; it never predicts a price.
- **A clinical decision.** It quotes what a label says, with the page, and refuses
  questions about a specific patient. Its labels are the versions in `corpus.json`.
- **Whether an appeal will win.** NADAC is a survey of what pharmacies paid; your PBM
  contract terms and appeal windows aren't in the data.
- **Anything newer than the last `fetch`**, and drug shortages at all.
- **Real reimbursement results from the sample data.** The claims and payer fees in
  `data/` are synthetic until you replace them.

Timing an order around a price change is worth cents per drug; the money is in the
totals across the whole shelf.

## Exit codes

`0` ok · `1` no usable rows · `2` bad input, or an optional package not installed · `3` bad rule · `4` download or service failed
