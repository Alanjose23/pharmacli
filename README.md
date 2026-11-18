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
- **what the labels say about one patient's drugs taken together**, for a signed-in pharmacist: every pair in a category with the label sentence behind it, the label sections that match the patient's readings, and recalls and cheaper generics for their drugs. Synthetic patients only (see [Patient profiles](#patient-profiles-pharmacist-sign-in-and-the-patient-view)).
- **a patient's own page**: their medication list, each label's counseling section word for word, and recall notices.

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
| *(core)* | `fetch`, `watch`, `check`, `today`, `margin`, `reorder`, `lookup`, `ask` (the 14 questions; find-a-drug matches words in the drug name), `claims`, `generics`, `recalls`, `clean`, `bench --workload io/cpu`, `patients account/mock/list`, `docs classes` |
| `.[analysis]` | `forecast`, `nlp --task baseline`, `bench --workload numpy` |
| `.[analysis,track]` | `sweep` (MLflow). `dvc` is a separate install (`pipx install dvc`), used by hand to version the database |
| `.[serve]` | `chat`, the API's document and health endpoints, and the sign-in, pharmacist and patient pages |
| `.[analysis,serve]` | the API's `/predict` as well (`.[analysis,serve,rag]` for the document endpoints). `docker compose up` installs all three itself |
| `.[analysis,track,dl]` | `nn`. Install torch from the CPU index first: `pip install --index-url https://download.pytorch.org/whl/cpu torch` |
| `.[analysis,nlp]` | `nlp --task finetune/search`, and matching by meaning in `ask "find a drug by description"` ("cholesterol pill" → Lipitor) |
| `.[rag]` | `docs`, `interactions`, document answers in `ask`, and `patients report/warm` (a report quotes label PDFs). Also needs Chroma and Ollama (see below); the pair checks need only Ollama |
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

### 5. Patients (uploaded in the pharmacist page)

**Synthetic patients only.** One row per patient, read **by header name**. `name`, `dob`
and `drugs` are required; every other column may be left out or blank. The page offers
the template (`/static/template.csv`), and `pharmacli patients mock -o FILE` writes 25
generated patients in this format.

| column | meaning | accepted |
|---|---|---|
| `name` | the patient's name | up to 120 characters |
| `dob` | date of birth, `YYYY-MM-DD` | 1900-01-01 to today |
| `drugs` | drug names separated by `;` | 1 to 30 names; a name with no label is kept and flagged |
| `systolic`, `diastolic` | blood pressure, mmHg | 50–300 and 20–200, both or neither, diastolic below systolic |
| `egfr` | mL/min/1.73 m² | 0–200 |
| `crcl` | creatinine clearance, mL/min | 0–300 |
| `alt`, `ast` | liver enzymes, U/L | 0–10,000 |
| `potassium` | serum potassium, mmol/L | 1–10 |
| `weight_kg` | kg | 0.5–500 |
| `pregnant` | `yes` or `no` | |
| `allergies` | free text | up to 500 characters |
| `taken` | the date of these readings, `YYYY-MM-DD` | from the date of birth to today; blank means today |

- The ranges are checks on data entry (a negative weight, a diastolic above the
  systolic), not clinical thresholds. A row outside them is rejected and reported with
  its line number and the reason; the other rows are stored.
- A row whose name and date of birth match a patient already on file (ignoring case,
  spacing and accents) adds its readings and any new drugs to that patient, and is
  counted as *updated*, not *created*. Uploading the same file twice adds nothing.
- Nothing is overwritten: a new value is a new reading, and the history keeps every one
  with its date and who entered it.

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
| `ask "question"` | The 14 questions below, in plain English. `ask` alone lists them. Name the drug in the question, by NDC or by name (`ask "what has lipitor's price done"`): a name picks the newest file's drugs that match the most of its words, your shelf's first, and your busiest NDC of that name; a name that fits several drugs lists them. `--ndc` / `--phrase` override the sentence |
| `ask -o FILE.csv` | Every shelf drug's answers, one row each |
| `chat` | `ask` in a browser at `127.0.0.1:8765` (`--port`, `--no-open`). Attach an inventory CSV to ask about that file instead of your saved shelf, then save it to the shelf if you want to keep it. Every question is in the menu above the box; the ones about a drug wait in the box for you to name it. A drug answer offers the other drug questions about it as one-click follow-ups, and a label answer appears as it is written (it streams from `/ask/stream`). Runs the API on localhost and opens the page with a token in the URL fragment; `--no-open` prints that URL to copy. The link works for 12 hours. Needs `.[serve]` |

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
| `interactions DRUG DRUG…` | For 2–6 drugs, asks the documents about every pair and prints the answer with its label citations. Drugs with no label are listed as not checked. An answer with no interaction does not mean the drugs are safe together. `--category` also sorts each pair into one of the four categories below, with the label sentence it rests on |

### Patient profiles (synthetic patients only)

| command | does |
|---|---|
| `patients account NAME` | Creates a pharmacist account. The password is typed at a prompt that does not echo, never passed as an argument; at least 12 characters, no other rules. There is no default account |
| `patients mock` | Loads 25 generated patients (fixed seed, plainly fictional names, 2–6 labeled drugs each) through the same reader as an upload. `-o FILE` also writes the CSV. Running it again updates, never duplicates |
| `patients list` | Patient ids, names, dates of birth and drug counts |
| `patients report ID` | One patient's report as text, from the cache, and how long it took to build |
| `patients warm` | Checks every pair the patients on file still need, one at a time, and stores each answer. Minutes to hours on a CPU: leave it running. Stop it whenever; it carries on where it stopped |
| `docs classes` | Looks up each corpus drug's pharmacologic classes in openFDA's NDC directory and keeps them in `classes.json`. Only drugs not yet in the file are asked about |
| `docs eval --pairs` | Scores the drug-pair answer key (`tests/pairs_qa.jsonl`): agreement, severe pairs placed milder (the target is none), and pairs sent to manual review |

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
| `GET /login`, `/pharmacist`, `/patient` | open | the sign-in page and the two signed-in pages. Shells with no data in them, served with `Content-Security-Policy: default-src 'self'` |
| `GET /static/{name}` | open | the token stylesheet (`theme.css`), the page scripts, and the patient upload template. A fixed table of files, not a folder |
| `POST /auth/login` | open | `{username, password}` → a token carrying a role and a server-side session. An unknown name and a wrong password get the same answer; repeated failures lock the name for a while |
| `POST /auth/logout`, `POST /auth/password` | signed in | ends the session; changes the password (a one-time password must be changed before anything else works) |
| `GET /patients`, `POST /patients`, `POST /patients/upload`, `POST /patients/mock`, `GET /patients/questions` | pharmacist | the patient list (with the override counts), a patient entered by hand, a CSV upload, the mock patients, and the questions patients passed on |
| `GET /patients/{pid}`, `POST …/readings`, `POST …/drugs`, `DELETE …/drugs/{drug}`, `GET …/report`, `POST …/decisions`, `POST …/login` | pharmacist | one profile with its history; a new reading; a drug added or stopped; the report; accept or override one item; create or reset the patient's login |
| `GET /patients/me`, `…/me/counseling`, `…/me/recalls`, `POST …/me/ask`, `POST …/me/forward` | patient | the caller's own medication list, counseling text and recall notices; a label question; passing a question to the pharmacists. No id in these paths: the record is the one the session names |

**Who may call what.** A token is one of three kinds:

| token | from | works on |
|---|---|---|
| no role | `pharmacli token`, `pharmacli chat` | everything it worked on before (`/predict`, `/ask`, `/ask/stream`, `/chat/*`); refused (403) on every route under `/patients` |
| `pharmacist` | signing in at `/login` | every route |
| `patient` | signing in at `/login` | only the `/patients/me` routes; refused (403) on `/predict`, `/ask`, `/ask/stream`, every `/chat/*` route and every pharmacist route |

Signing in records a session on the server, so signing out, the end of the session
(`PHARMACLI_SESSION_HOURS`, 8 by default) and a reset password all end the token
before its own expiry. Every route under `/patients` is on one of two routers, each
with its role check as a dependency of the router, and that same check writes the audit
row, allowed or denied. `tests/test_auth.py` walks the route table and fails if a route
under the prefix is not behind its guard.

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

`path` is the route template (`/patients/{pid}/report`), not the URL, and `/metrics`
groups by the same template, so a patient's id is in neither. No request body, name or
date of birth is logged. Who opened or changed which patient is in the `audit` table of
the patient database instead: who, role, action, patient id, request id, time, and
whether it was allowed. That table cannot be updated or deleted from; a trigger aborts
both.

`/metrics` also carries `decisions` (how many report items pharmacists accepted and
overrode) and `new_patient` (whether the first submit of the new-patient form was
valid, and the seconds from first keystroke to save). Both are counts with nothing
about a patient in them.

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

## Patient profiles, pharmacist sign-in and the patient view

**Synthetic patients only.** Storage, access control and audit logging here are not
ready for protected health information under HIPAA, and nothing in this tool claims to
be. Every patient it ships with is generated, and the pharmacist page says so on screen.

```bash
pip install -e ".[serve,rag]"
pharmacli docs fetch                          # the label PDFs a report quotes (first run only)
pharmacli patients account alice              # a pharmacist; type a password of 12+ characters
pharmacli patients mock                       # 25 generated patients
docker compose up -d --wait chroma ollama     # for pair categories and label questions
uvicorn pharmacli.serve:app --port 8000       # then open http://127.0.0.1:8000/login
```

Sign in as `alice` and open a patient. To see the patient's side, open "give the
patient a login" on a profile, pick a username, and hand over the one-time password
it shows; sign out, sign in with it, choose a new password, and the patient's own page
opens.

### What a pharmacist sees

A bar at the top of the page adds a patient by hand, by keyboard alone: name, date of
birth, drugs (Enter after each), and optional indicators. A drug name with no label is
flagged as it is typed and saved as flagged. The same name and date of birth as a
patient on file, whatever the case, spacing or accents, has to be confirmed as a second
person; the server makes that check, not only the page.

Opening a patient shows the drugs with the label each matched, the current indicators,
the history of every reading with who entered it, and a report:

- **Every pair of the patient's drugs, in a category** taken from what the two labels
  say, most severe first: *Do not use together*, *Avoid or adjust the dose*, *Monitor*,
  *No interaction stated in the labels* (which is not the same as safe). Three statuses
  are not categories: *Needs manual review* (a check failed; listed above everything),
  *Not checked* (a drug has no label) and *Pending* (not run yet). The report says
  "N of M pairs checked" and names the rest.
- **Label sections matched to the patient's readings**, quoted with their pages and
  with the patient's value beside them. No model is involved.
- **Shelf checks for the patient's drugs**: a cheaper generic, citing the NADAC file,
  and FDA recalls, citing the recall number.
- **Accept or override** on every item, with an optional reason. Decisions are kept
  append-only, and the page header shows how many were overridden.

### How a pair gets its category

1. **Search both labels, in plain text.** For drugs A and B, A's labels are searched
   for B and B's for A: Contraindications, the boxed warning, Drug Interactions,
   Warnings and Precautions, and Dosage and Administration (where dose adjustments
   sit). The 14 labels in the older format have no numbered sections and are searched
   whole. The search looks for the other drug's names and for its pharmacologic class
   as labels write it ("strong CYP3A4 inducers", "ACE inhibitors"), from `classes.json`.
2. **Nothing found in either label** is the only way to reach *No interaction stated in
   the labels*. No model is asked; the citation lists the sections and pages searched.
3. **Otherwise the model gets a small job.** It sees up to 8 numbered sentences per
   label (those naming the drug before those naming only its class) and answers with a
   sentence number and one category. Code copies that sentence out of the label, so
   the quote on screen is the label's own words, with document · section · page ·
   label date.
4. **The answer is checked before it is shown.** Any of these sends the pair to *Needs
   manual review*, with the sentences that were found still shown and cited: an answer
   that is not one of the categories; a sentence number the model was not given; a
   sentence that does not name the other drug or its class; "no interaction" where the
   search found a mention; "do not use together" resting on a sentence that does not
   say so; a category milder than the words of the sentence it rests on, or of any
   sentence naming the drug; and a drug with no class on file when nothing else was
   found. Nothing is ever moved to a milder category.
5. **The more severe of the two labels' readings stands.**

A pair's answer is about two labels, so it is cached under both label versions, the
prompt version and the model, and reused for every patient. The queue is not a table:
it is every pair some patient needs minus what is cached, so it resumes by itself after
an interruption and a label refreshed by `docs refresh` puts its pairs back in. The API
checks queued pairs one at a time in the background and stands aside while a live
question is being answered; `pharmacli patients warm` does the same from the terminal.

`classes.json`, the indicator triggers in `review.py` and the pair answer key are
**provisional and unreviewed**: each entry names its source, and only a pharmacist can
sign them off.

| reading | brings up | when | source |
|---|---|---|---|
| eGFR | the label's renal impairment sections | below 60 mL/min/1.73 m² | KDIGO 2024 CKD guideline: GFR categories G3a to G5 |
| creatinine clearance | the same | below 60 mL/min | FDA guidance, Pharmacokinetics in Patients with Impaired Renal Function (March 2024), Table 1 |
| ALT, AST | the label's hepatic impairment sections | above 40 U/L | ABIM Laboratory Test Reference Ranges (January 2025) |
| pregnant: yes | section 8.1 | | 21 CFR 201.57(c)(9)(i) |

A trigger only chooses what to show; the guidance is the label's words. Blood pressure,
potassium, weight and allergies are recorded and displayed and trigger nothing, and
allergies are not checked against drugs.

### What a patient sees

Their medication list; section 17 of each drug's label, word for word with its pages,
from the function `docs handout` uses; and FDA recall notices for their drugs. No
indicators, no categories, no recommendations.

A patient can ask what a label says. The answer is label text as retrieved, quoted with
its citation; nothing is generated. A question is answered only if it is recognisably a
general question about a label: it names a drug that has one, and every other word is
on a short list that has no "I", "my" or "me" and no "can", "should" or "safe". So
"can I…", "should I…", "is it safe for me…" and "I missed a dose" are refused before
anything is retrieved, by a rule that does not depend on the model, and so is anything
else the rule does not recognise. The refusal points to the counseling text on the page
and offers to send the question to the pharmacists, who see it in a queue.

### Profiles beside a chat that refuses patient questions

The chat still refuses any question about a specific patient, exactly as before.
Profiles do not relax that rule; they go around the model. Nothing from a profile is
ever put into a prompt: a pair check asks the model about two labels, the label
sections matched to a reading are chosen by a table in code and quoted, and the shelf
checks are lookups. The patient's data selects which label text to show, a pharmacist
reads it, and the decision stays with the pharmacist. The model is still never asked
to decide anything about a person.

### Measured

On this laptop's CPU, with `qwen2.5:3b-instruct`, on the unreviewed 18-pair key
(`pharmacli docs eval --pairs`). These are provisional: the key has not been reviewed
by a pharmacist, and the prompt was revised while looking at five of its pairs.

| | result |
|---|---|
| Agreement with the key | 8 of 18. The 4 "nothing stated" rows agree by construction (the same search wrote the key); of the 14 pairs where a label says something, 4 agree |
| Severe pairs placed in a milder category | 0 of 10 (the target is none) |
| Pairs sent to manual review | 10 of 18. This is what the checks cost: the 3B model's first answer was "do not use together" for nearly everything, and the checks turn its wrong answers into manual review instead of showing them |
| Pairs shown in a wrong category | 0 |
| Pairs settled by the text search alone | 687 of the 990 possible pairs among the 45 drugs have no sentence in either label, found in 12 s with no model call. The other 303 need the model |
| A cached report for a six-drug patient (15 pairs) | 0.7 s (`pharmacli patients report ID`) |
| Patient-phrased questions refused by the gate | 23 of 23 (`tests/rag_qa.jsonl`, rows with `"gate": "patient"`) |
| Lighthouse accessibility | 100 on the chat, sign-in, pharmacist and patient pages |
| Width | no sideways scroll at 375 px on any of the four pages |

### Limits

- **Synthetic patients only**, and one pharmacy: any pharmacist can open any patient.
- **Most pairs where a label says something end in manual review** with this model.
  The category is right when it is shown; it is often not shown.
- **Label text comes out of PDFs.** A table is flattened into running text, so a quote
  from one can read oddly and carry a stray word from the next cell. The page number is
  there to check it.
- **A drug is matched by the first word of its generic name.** "losartan" matches the
  losartan and hydrochlorothiazide label, which is the only losartan label in the
  corpus; the page shows which label each drug matched.
- **Recalls are matched on the package codes of the labels in the corpus.** A recall of
  another manufacturer's version of the same drug is not matched.
- **The patient gate is strict.** A general question that uses a word outside its list
  ("can gabapentin be crushed") is refused and offered to the pharmacists.
- **Forwarded questions are listed, not managed**: there is no way yet to mark one answered.
- **Sign-in is for localhost.** Tokens are bearer tokens in `sessionStorage`; there is
  no password recovery beyond a pharmacist resetting a patient's login, and a
  pharmacist's password is changed by signing in and posting to `/auth/password`.

### The four improvements, as measured

**1. Green and white**

| Metric | Before | Target | Now |
|---|---|---|---|
| Text contrast (WCAG AA) | grey `#8A8A8A` at 3.45:1 | every text colour at least 4.5:1 | lowest text pair 4.79:1 (green on the tint); borders 4.62:1 against a 3:1 need (`tests/test_theme.py`) |
| Theme in one place | 14 hex colours inline | defined once | 9 tokens in `theme.css`; no hex colour in any page or script (same test) |
| Page weight | 11 KB | under 30 KB | chat 16.9 KB with its stylesheet; sign-in 10.8, pharmacist 28.2, patient 13.6 KB against 60 KB each; no other-origin URL |
| Phone layout | not tested | usable at 375 px | no sideways scroll at 375 px (headless Chrome) |
| Lighthouse accessibility | not measured | 90 or higher | 100 |

**2. Pharmacist sign-in, profiles and reports**

| Metric | Before | Target | Now |
|---|---|---|---|
| Accounts | anyone with a token | named pharmacists, salted hashes | `patients account`; scrypt with a 16-byte salt per account, N=2^17, r=8, p=1 |
| Roles | none | checked on every patient endpoint | `pharmacist` or `patient`, from the server-side session |
| Access-control tests | missing or expired token → 401 | no token, expired, wrong role on every patient endpoint | generated from the route table for every route: no token, a malformed one, expired, signed out, no session, no role, wrong role |
| Audit trail | a log line per request | who and request id for every read or change | append-only `audit` table, allowed and denied |
| Session length | 1 hour, or 12 for the chat link | one shift, with sign-out | 8 hours (`PHARMACLI_SESSION_HOURS`), `POST /auth/logout` |
| Mock patients | none | 25 from a fixed seed, every drug labeled | 25; the same seed gives the same bytes |
| Upload validation | inventory reader | every rejected row with line and reason | yes |
| Indicator history | — | every update kept with date and who | append-only by trigger |
| Patient data in logs | no bodies logged | none in logs or `/metrics`, by a test | canary test: name, date of birth and id absent from both |
| Category accuracy | — | 90% agreement with a pharmacist-labeled key | **not measured**: needs a pharmacist's key. Provisional: 8 of 18 on the unreviewed key |
| Missed severe interactions | — | none | 0 of 10 placed milder (provisional, same key) |
| Cited recommendations | uncited answers withheld | every one cites a label page | every category, section, generic and recall carries its source; what cannot be cited is manual review |
| Coverage | "not checked" listed | pairs checked out of total | "N of M pairs checked", with the pending and unchecked named |
| Time to a report, 6 drugs | 10 to 20 minutes | under 1 minute cached | 0.7 s cached (measured on one mock patient) |
| Pharmacist decisions | — | accepted or overridden, rate tracked | every item; counts on the page and in `/metrics`. The rate in real use is **not measured** |

**3. The new-patient bar**

| Metric | Target | Now |
|---|---|---|
| Time to add a patient | under 2 minutes | **not measured**: needs people. `/metrics` → `new_patient.seconds` records it per save |
| Valid on first submit | at least 90% | **not measured**: `/metrics` → `new_patient.first_submit_valid` / `first_submit_invalid` |
| Duplicate patients | flagged before saving, in every test case | case, spacing and accent variants all refused until confirmed (`tests/test_entry.py`) |
| Unmatched drug names | flagged at entry, none unflagged in a report | flagged as typed, saved as flagged, reported as "not checked" |
| Keyboard-only entry | the whole form | completed in headless Chrome with key presses only |

**4. Patient sign-in**

| Metric | Target | Now |
|---|---|---|
| Records a patient can see | only their own | their routes take no id; every pharmacist route answers another patient's id exactly as an id that does not exist (403) |
| Patient-specific questions refused | all, on an extended key | 23 of 23 patient-phrased rows refused by the gate; the chat's own 5 refusal rows are unchanged |
| Task completion | 90% in a usability test | **not measured**: see the script below |
| Counseling text | section 17 word for word | the same function as `docs handout`, per drug |

## Usability session script

One page, for a session with a pharmacist and one or two people playing patients.
Use the mock patients. Enter nobody real.

**Before.** `pharmacli patients mock`, `pharmacli patients warm` the night before, and
the service running. Create one pharmacist account. For each participant playing a
patient, open a mock patient, create a login and write the one-time password on a card.
Note the numbers under `new_patient` at `/metrics`.

**Part A: three patient tasks.** Hand over the card and say only: "This is your
pharmacy's page. Sign in." Then ask, one at a time, without pointing at the screen:

1. "Which medicines does the pharmacy have on file for you?"
2. "Find what the label for one of your medicines says you should be told about it."
3. "Is there a recall on any of your medicines?"

For each task write down: done without help, done with a hint, or not done; and the
first thing they clicked. A task counts as completed only without help. Afterwards ask
them to type one question they would really ask. Write down the question and whether it
was answered, refused, or sent on. The target is 90% of tasks completed.

**Part B: adding a patient by hand.** Give the pharmacist a card with a made-up name, a
date of birth, three drugs (one of them not in the corpus) and a blood pressure. Say:
"Add this patient. Use the keyboard only." Do not explain the form. Write down whether
the unmatched drug's flag was noticed, and whether anything had to be asked. Repeat with
a second card that has the same name and date of birth as the first, in different
capitals.

**After.** Read `/metrics` again. `new_patient.seconds` gives the time from first
keystroke to save (target: under 2 minutes) and `first_submit_valid` against
`first_submit_invalid` gives the share valid on the first submit (target: 90%).
`decisions` gives the override count if the pharmacist also worked through a report.

## Where the data lives

| path | what it holds | written by |
|---|---|---|
| `pharmacli.db` | SQLite. `nadac`: one row per NDC × effective date × weekly file (price, pricing unit, rate class, OTC, generic price, explanation code). `watch`: your shelf (ndc, cost, on_hand, monthly). `package`: openFDA pack sizes | `fetch`, `watch`, `watch packs` |
| `pharmacli.db.dvc` | DVC pointer to a snapshot of the database; `dvc checkout` restores it from the local DVC cache (no remote is configured). DVC is not a project dependency: install it yourself to use these files | `dvc add` |
| `patients.db` (`PHARMACLI_PATIENT_DB`) | SQLite, **synthetic patients only**. `patient`, `drug`, `reading` (indicator history), `account`, `session`, `attempt` (failed sign-ins), `audit`, `pair` (cached label-pair answers; no patient in them), `decision`, `question`. `audit`, `reading` and `decision` are append-only by trigger. Git-ignored, in `.dvcignore` and `.dockerignore`; under compose it lives on the `patient-data` volume, not in `./data` | `patients account`, `patients mock`, the pharmacist page |
| `classes.json` | For each corpus drug, openFDA's pharmacologic classes, plus the other ways labels write those classes. **Provisional and unreviewed**; every entry names its source | `docs classes`, and by hand |
| `tests/pairs_qa.jsonl` | The drug-pair answer key: 18 pairs, each expected category tied to a label passage (document, page, quote). Unreviewed | by hand, from the label text |
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
| `patients.py` | The patient database: schema and append-only triggers, scrypt password hashes, sign-in with lockout, sessions, the audit trail, profile validation, the upload reader, the mock-patient generator, decisions. Standard library only |
| `review.py` | What a pharmacist reviews: which label a typed drug means, the sentences in each label that name the other drug or its class, the model's category with the checks on it, the cache and the queue derived from it, indicator triggers, per-patient shelf checks, the report, and the gate on a patient's question |
| `theme.css` | The colour tokens (the only place a colour is written) and the shared layout rules |
| `login.html`, `pharmacist.html`, `patient.html` | The sign-in page and the two signed-in pages: markup only, no inline script or style |
| `app.js`, `pharmacist.js`, `patient.js` | The shared helper (token in `sessionStorage`, sent in the `Authorization` header; every dynamic value inserted as text) with the sign-in logic, and each page's own script. The patient page loads no pharmacist code |

`tests/` holds the pytest suite: `pip install -e ".[analysis,serve,rag]"` then `pytest -q`.
`tests/loadtest.py` and `tests/make_demo.py` are not part of it — the first needs the
service running (see **The service**), the second regenerates the committed demo fixture.

Lint with `ruff check src tests` (`pip install ruff`; the rules are in `pyproject.toml`)
and types with `mypy src/pharmacli`. CI runs the suite on 3.10/3.11/3.12, lints once,
and builds the image, curls its `/health` to prove the container still starts, and
requests every page, the stylesheet and each script from it (a static file left out of
the package data works from a checkout and is missing from the image).

Outside `src/`: `Dockerfile` (two stages, the runtime copies only the built venv),
`docker-compose.yml` (api + Chroma + Ollama, and a `patient-data` volume for the patient database) and `.dockerignore` (without it the build
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
| `PHARMACLI_PATIENT_DB` | `patients.db` | patient profiles, accounts and the audit trail: a SQLite file of its own, never the price panel. Synthetic patients only |
| `PHARMACLI_SCRYPT_N` | `131072` | scrypt cost N for password hashes, at r=8 and p=1 (2^17, the OWASP minimum) |
| `PHARMACLI_SESSION_HOURS` | `8` | how long a sign-in lasts: one working shift |
| `PHARMACLI_LOCK_AFTER` | `5` | wrong passwords for one name, inside the window below, before sign-in is locked for that name |
| `PHARMACLI_LOCK_MINUTES` | `15` | the window, and how long the lock lasts |

## Commands that delete or overwrite data

- `fetch --fresh` deletes the database before downloading, without asking.
- `bench` with `--workload io` or the default `both` deletes the `--db` file four
  times per run (default: a file in the temp folder). Never point `--db` at `pharmacli.db`.
- `watch import` replaces the stored cost for every NDC in the file.
- `claims sample` overwrites `data/claims_sample.csv` and `data/payers.csv` (or the `-o`/`--payers` paths).
- `docs fetch --from-shelf` overwrites `corpus.json`; `docs refresh` rewrites it and replaces PDFs.
- `sweep` without `--sample` replaces `models/pharmacli-mover.joblib` and `.json`.
- `docs classes` rewrites `classes.json`, keeping what is in it and adding drugs it has not looked up.
- Nothing in the patient database is overwritten by the tool: readings, decisions and
  audit rows are append-only, and a stopped drug is kept. Deleting `patients.db` deletes
  every profile, account and audit row.

## What it can't answer

- **A future price.** It gives the odds that a price moves and when; it never predicts a price.
- **A clinical decision.** It quotes what a label says, with the page, and refuses
  questions about a specific patient. Its labels are the versions in `corpus.json`.
  A patient's report sorts label sentences into categories and puts label sections
  beside the patient's readings; a pharmacist accepts or overrides each one.
- **Whether two drugs are safe together.** "No interaction stated in the labels" says
  what two documents contain, which is not the same as safe, and the tool's own wording
  never calls anything safe.
- **Anything about a real patient.** The profiles are synthetic; the storage, access
  control and audit logging are not ready for protected health information under HIPAA.
- **Whether an appeal will win.** NADAC is a survey of what pharmacies paid; your PBM
  contract terms and appeal windows aren't in the data.
- **Anything newer than the last `fetch`**, and drug shortages at all.
- **Real reimbursement results from the sample data.** The claims and payer fees in
  `data/` are synthetic until you replace them.

Timing an order around a price change is worth cents per drug; the money is in the
totals across the whole shelf.

## Exit codes

`0` ok · `1` no usable rows · `2` bad input, or an optional package not installed · `3` bad rule · `4` download or service failed
