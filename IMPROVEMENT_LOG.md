# Improvement log

Working log for the run that carries out `IMPROVEMENT_BRIEF.md` (the copy used is
`~/Downloads/IMPROVEMENT_BRIEF.md`; it was attached to the session, not placed in the
repository). Started 2026-11-16 from commit `9d94feb` on `main`, with seven tracked
files already modified in the working tree (the owner's own uncommitted edits to
`README.md`, `ask.py`, `chat.html`, `cli.py`, `serve.py`, `test_ask.py`, `test_chat.py`).

## Environment

| Question | Answer |
|---|---|
| Python and the extras | Python 3.12.3 in `.venv`, with `fastapi`, `uvicorn`, `httpx`, `pymupdf`, `pandas`, `scikit-learn`, `pytest`, `ruff` 0.16.8 and `mypy` 2.3.1 already installed. Only 3.12 is on the machine; 3.10 and 3.11 were not run |
| Ollama and Chroma | Not running at the start. Docker is installed, and the `ollama/ollama:0.34.0` and `chromadb/chroma:1.5.9` images and both named volumes exist, so `docker compose up -d chroma ollama` can start them |
| Label PDFs | All 56 present in `data/docs/` (57 MB). All 50 inserts parse in 5.0 s in total |
| Headless browser | `google-chrome` and `chromium-browser` are installed; Node 24 and `npx` are available. Lighthouse is not installed (the npm registry is reachable) |
| Docker | Available (`docker ps` works). A `pharmacli-api` image from an earlier build exists |
| Network | `api.fda.gov` and `registry.npmjs.org` are reachable |

## Baseline

Recorded before any edit, on the working tree as found.

| Check | Command | Result |
|---|---|---|
| Lint | `ruff check src tests` | `All checks passed!` |
| CLI smoke | `pharmacli --help`, `columns`, `rules`, `clean fixture.csv --rule dedup --dryrun`, `clean … --rule sort:by=date --dryrun` | all exit 0 |
| Benchmarks | `pharmacli bench --workload cpu --workers 2`, `pharmacli bench --workload numpy` | both exit 0 |
| Tests | `pytest -q` | `189 passed, 1 warning in 8.23s` (the warning is Starlette's deprecation of `httpx` in its test client) |
| Types | `mypy src/pharmacli` | `Found 9 errors in 5 files (checked 31 source files)`, all in `rules/__init__.py`, `claims.py`, `track.py`, `nn.py` and `nlp.py`. mypy is not a CI step. Left as found |

## Decisions

Choices made beyond section 6 of the brief, one line each, in the order they came up.

- **No commits and no new branch.** The owner's standing instruction for this project is that they make every commit themselves and that branches need their go-ahead. That instruction is specific to this repository and the brief's commit steps are generic, so the work is left as uncommitted changes on `main`. Each milestone was checked with the full suite and a copy of the tree was kept outside the repository as the checkpoint.
- **`PROJECT_SUMMARY.md` was not recreated.** See "Differences from the summary".
- **Class terms live in a committed file, `classes.json`**, not a cache table: they are reference data a pharmacist must review, like `corpus.json`, and the image needs them. `pharmacli docs classes` fills it from openFDA (45 lookups, once); 6 drugs openFDA gives no class for got hand-written terms, each quoting the label sentence it comes from.
- **The queue is derived, not stored**: every pair some patient needs, minus what is cached under the current label versions, prompt and model. Resuming, re-checking after a label refresh and "N of M" all fall out of that, with no queue table to keep consistent.
- **The model picks a sentence number and a category; code checks both.** Every way the answer can be wrong (out-of-set, a sentence it was not given, a sentence not naming the drug or class, "no interaction" against the search, "do not use together" on a sentence that does not say so, a category milder than the sentence's own wording) ends in "Needs manual review". The wording check uses two provisional lists of words (forbid; avoid or adjust the dose) that can only move a pair to review, never set a category.
- **On a failed check the sentences found are still shown,** cited and with no category claimed, so manual review starts from the label text and not from nothing.
- **A patient's question is answered with retrieved label text, not generated text,** and only when an allow-list gate recognises it as a general label question. The gate cannot be paraphrased past: what it does not recognise is refused.
- **Patient-phrased refusal rows are scored against the patient gate**, not the chat: patients never reach the chat, and the chat's own 50-row numbers stay exactly as before (the rows carry `"gate": "patient"`).
- **The model prompt was revised once (version 2 to 4) after looking at the five key pairs**, because version 1 answered "A, sentence 1" to everything. That means the provisional score below is not independent of those five pairs.
- **Weight budgets count what a page loads** (`<link>` and `<script>`), not the template CSV it links for download.

## Milestones

### Milestone 1: the green-and-white theme

Plan: one stylesheet (`theme.css`) holding the nine tokens and the shared base rules, served
from a fixed table at `/static/{name}`; `chat.html` links it and keeps only layout rules,
with its script unchanged apart from the busy marker; a test file that reads the tokens.

| Check | Command | Result |
|---|---|---|
| Tests | `pytest -q` | `198 passed, 1 warning in 8.18s` (189 before, 9 new in `tests/test_theme.py`) |
| Lint | `ruff check src tests` | `All checks passed!` |
| Contrast | `tests/test_theme.py` reads the tokens | ink 17.74 / 16.95, ink-muted 7.56 / 7.22, green 5.02 / 4.79, green-strong 7.13 / 6.81, danger 6.47 / 6.18, caution 7.09 / 6.77 (on white / on tint); white on green 5.02; line 4.83 / 4.62 |
| One place for colours | same test | 9 hex values, all in `theme.css`; none in any page or script |
| Page weight | same test | chat 13.8 KB with its stylesheet, against a 30 KB budget; no other-origin URL |
| Lighthouse accessibility | `npx lighthouse@12 http://127.0.0.1:8799/chat#token=… --only-categories=accessibility` | Lighthouse 12.8.2, score `1` (100), no failing audit |
| 375 px | headless Chrome, 375 × 800, after asking a question and opening the menu | `scrollWidth 375`, `innerWidth 375`, no element wider than the viewport |

Finished: milestone 1.

### Milestone 2: accounts, roles, sessions and audit

Plan: a second SQLite file (`patients.db`, path from `PHARMACLI_PATIENT_DB`) opened by a
new standard-library module `patients.py` holding accounts, sessions, sign-in attempts and
the append-only audit table. `serve.py` gains `/auth/login`, `/auth/logout` and
`/auth/password`, one guard per role applied to two routers under `/patients`, the
sign-in page and its script, and the route-template fix for `/metrics`.

| Check | Command | Result |
|---|---|---|
| Tests | `pytest -q` | `226 passed, 1 warning in 11.72s` (28 new: `tests/test_patients.py`, `tests/test_auth.py`, and three more pages in `tests/test_theme.py`) |
| Lint | `ruff check src tests` | `All checks passed!` |
| Types | `mypy src/pharmacli` | `Found 9 errors in 5 files (checked 32 source files)`: the same nine as the baseline, none in the new code |
| Guard test is not vacuous | a scratch script adds `GET /patients/zzz/leak` straight onto the app and calls the route-table test | `CAUGHT: /patients/zzz/leak is not behind the pharmacist guard` |
| Compose file | `docker compose config --quiet` | exit 0 |
| Sign-in by keyboard | headless Chrome at 375 px: type the name, Tab, a wrong password, Enter, then the right one, Enter | the error appears beside the form with focus back on the password; then `/pharmacist` loads, the URL carries no token, and `sessionStorage` holds it; `scrollWidth 375` |

What the walk found: FastAPI 0.141 no longer copies an included router's routes into
`app.routes`, so a test that only read `app.routes` passed while checking nothing. The
route-table test now walks into included routers, cross-checks against the published
OpenAPI paths, and asserts that it found both kinds of route.

Finished: milestone 2.

### Milestone 3: profiles, mock patients and upload

Plan: profile reads and writes in `patients.py` (already append-only by trigger), the
label match for a typed drug name in a new `review.py`, the generator for 25 mock
patients writing the same CSV the upload reads, and the pharmacist routes for the list,
a profile, new readings, drugs, the upload and the mock load. The pharmacist page gets
the list, the profile with its history and the upload with its reject report.

| Check | Command | Result |
|---|---|---|
| Tests | `pytest -q` | `244 passed, 1 warning in 14.48s` (18 new: profiles, upload, matching and mock patients in `tests/test_patients.py`, and `tests/test_profiles.py`) |
| Lint | `ruff check src tests` | `All checks passed!` |
| Mock patients | `pharmacli patients mock -o mock.csv`, twice, on a scratch database | `25 mock patients created, 0 updated`, then `0 mock patients created, 25 updated` |
| Browser | headless Chrome, signed in, mock patients loaded, first patient opened, at 375 px and 1200 px | focus moves to the profile heading; `scrollWidth` equals `innerWidth` at both widths |

Decisions:

- **A drug is the first word of a generic name.** That is the key the existing name
  resolution matches on, and it makes the corpus's 50 labels the summary's 45 drugs. A
  drug with two labels (potassium chloride, atorvastatin, pregabalin, rosuvastatin,
  venlafaxine) is checked against both.
- **An exact generic or brand name wins before the word match.** Without it "losartan
  potassium and hydrochlorothiazide" matches two drugs (losartan and potassium) and
  would be flagged although it has a label.
- **A drug is stopped, not deleted,** and re-uploading the same file adds no reading: a
  reading with the same indicator, value and date is the same reading.
- **An upload row that matches two existing patients is rejected** with its line number.
  Two records can share a name and birthday once a pharmacist has confirmed the second,
  and appending to one of them would be a guess.
- **Sanity bounds** (data-entry checks, not clinical thresholds): systolic 50 to 300 and
  diastolic 20 to 200 mmHg with diastolic below systolic, eGFR 0 to 200, creatinine
  clearance 0 to 300, ALT and AST 0 to 10,000 U/L, potassium 1 to 10 mmol/L, weight 0.5
  to 500 kg, a date of birth from 1900 and not in the future, and a reading date
  between the date of birth and today.

Finished: milestone 3.

### Milestone 4: interaction categories and recommendations

Plan: in `review.py`, find the sentences in each label's Contraindications, Warnings and
Precautions, Drug Interactions, boxed warning and dosage sections that name the other
drug or its class (`classes.json`, built from openFDA's `pharm_class`); give the model
those numbered sentences and take back one category and one sentence number; check the
answer and copy the quote from the label; cache by label versions, prompt and model in
the patient database; derive the queue as "pairs patients need minus pairs cached".
Then indicator triggers, shelf checks, decisions, the report route and command, and the
pair answer key scored by `rageval.py`.

| Check | Command | Result |
|---|---|---|
| Tests | `pytest -q` | 25 tests in `tests/test_review.py` with a stand-in model: each category, each failed check, label disagreement, the "nothing stated" guard, cache hit/miss/invalidation by label version, prompt and model, the queue resuming after an interruption and standing aside for a live question, "N of M" with pending and unchecked named, indicator sections, shelf checks, decisions and the override counts, and no patient field in any prompt |
| Text search over every pair | a scratch script over the 990 pairs of the 45 drugs | `990 pairs searched in 12 s`; 687 have no sentence in either label, 72 name the other drug, 231 only its class |
| Pair key, real model | `pharmacli docs eval --pairs` (Ollama `qwen2.5:3b-instruct`, CPU) | `agree 8` of 18; `stated_agree 4` of 14; `severe_placed_milder 0` of 10; `manual_review 10`; 2m 8s |
| Warm-up | `pharmacli patients warm` on a scratch database holding one six-drug mock patient | `15 pairs checked in 1.1 minutes (4 s a pair)` |
| Cached report | `pharmacli patients report ID`, three runs | `report built in 0.73 s` / `0.79 s` / `0.66 s` `from the cache (15 of 15 pairs checked)` |
| Background worker | the API with the same patient uploaded | all 15 pairs checked 72 s after upload, one at a time; the log line carries only `"checked": 15` |

The pair key (`tests/pairs_qa.jsonl`, 18 rows) was built by a script that finds each
row's passage in the parsed label text and stops if it is not there, so every quote and
page is the label's own. Its five starting pairs are the summary's; the expected
categories for two of them differ from what the summary implies: the labels put
aripiprazole + fluoxetine and lamotrigine + carbamazepine under dose adjustment, not
"do not use together".

Finished: milestone 4.

### Milestone 5: the new-patient bar

Plan: `POST /patients` reusing the upload's validation (one function, `patients.clean`)
with an error per field and a server-side duplicate check that needs `confirm: true`;
the bar at the top of the pharmacist page, drugs entered with Enter and checked as typed
against the same words the server matches on; two anonymous counters in `/metrics`.

| Check | Command | Result |
|---|---|---|
| Tests | `pytest -q tests/test_entry.py` | 5 tests: required fields, an error per field, duplicates across case, spacing and accents (refused until confirmed), an unmatched drug flagged through to the report, the audit row, the two measures and the patient login a pharmacist creates |
| Keyboard only | headless Chrome, 1200 px, key events only: name, Tab, date typed digit by digit, Tab ×2, two drugs each followed by Enter, Tab ×4, Enter | the unmatched drug is flagged as it is entered; saved; the profile opens with focus on its heading and lists `unobtainium · no label, not checked`; the report says `0 of 1 pairs checked; not checked, no label: unobtainium`; `/metrics` → `new_patient: {'first_submit_valid': 1, 'seconds': {'n': 1, …}}` |

Finished: milestone 5.

### Milestone 6: patient sign-in and view

Plan: a pharmacist creates the login (`POST /patients/{pid}/login`, a one-time password
that must be changed first); `/patients/me`, `/me/counseling` (the `docs handout`
function), `/me/recalls`, `/me/ask` (gate, then retrieved label text) and `/me/forward`
(the pharmacists' queue), none taking an id; 23 patient-phrased refusal rows.

| Check | Command | Result |
|---|---|---|
| Tests | `pytest -q tests/test_patient_view.py` | 8 tests: every pharmacist route called with one patient's token and another patient's id answers exactly as an id that does not exist; money routes refuse a patient token (in `tests/test_auth.py`); the forced password change; all 23 patient-phrased rows refused; a forwarded question in the pharmacist queue, audited; counseling text identical to `docs handout` |
| Patient gate on the real corpus | `pharmacli docs eval --retrieval-only` | `patient_rows 23`, `patient_refused 23`; the document lane's own numbers unchanged: `answerable 40`, `hit 36`, `routing 49` of 50 |
| Browser, 375 px | headless Chrome: one-time password, new password, the page, a general question, a personal question, "send to my pharmacist" | 6 medicines, 6 counseling sections, recall notice; the general question answered with label text and its citation; the personal one refused with the offer; the question appeared in `/patients/questions`; `scrollWidth 375` |

Finished: milestone 6.

### All milestones: the final checks

| Check | Command | Result |
|---|---|---|
| Lint | `ruff check src tests` | `All checks passed!` |
| CLI smoke | `pharmacli --help`, `columns`, `rules`, `clean … --rule dedup --dryrun`, `clean … --rule sort:by=date --dryrun`, `bench --workload cpu --workers 2`, `bench --workload numpy` | all exit 0, same output as the baseline |
| Tests, 3.12 | `pytest -q` | `282 passed, 1 warning in 23.28s` (189 at the start) |
| Tests, 3.10 | `uv run --python 3.10 --with-editable ".[analysis,serve,rag]" python -m pytest -q` in a throwaway environment | `276 passed, 1 skipped` (the skip is `test_nn.py`: torch is not in the CI extras either) |
| Python 3.11 | — | not run: not installed. 3.10 is the floor and 3.12 the newest, and no feature newer than 3.10 is used |
| Types | `mypy src/pharmacli` | `Found 9 errors in 5 files (checked 33 source files)`: the same nine as the baseline |
| Lighthouse accessibility | Lighthouse 12.8.2: the chat by URL; the other pages signed in, through a Puppeteer flow | `/chat 1`, `/login 1`, `/pharmacist (list) 1`, `/pharmacist (profile and report open) 1`, `/patient 1`: 100 everywhere, no failing audit |
| 375 px | headless Chrome on all four pages, with content loaded | `scrollWidth` equals `innerWidth` (375) on each |
| Docker | `docker build`, `docker run -e PHARMACLI_JWT_SECRET=ci`, then the CI step's requests | `smoke ok: pharmacli-mover-demo/0 over 276 NDCs`; `/chat`, `/login`, `/pharmacist`, `/patient`, `/static/theme.css`, `app.js`, `pharmacist.js`, `patient.js`, `template.csv` all 200; the CSP header on `/pharmacist`; no `patients.db` anywhere in the image; `/patients` without a token 401 |
| Compose | `docker compose config --quiet` | exit 0 |

The review pass (section 8 of the brief) was done by reading the diff three times, once
for each reader the brief names. What it changed: a table quote that pinned the row
above's instruction on the next row's drug class (fixed, with a test); a category that
could be milder than the wording of the sentence the model chose (now manual review);
one missing label PDF hiding every other drug's indicator sections (now per drug); a
list rendering that printed "null" and "[object HTMLElement]" (fixed, and a test bans
the pattern). What it found and left is under "Noticed, not changed".

## Differences from the summary

- `PROJECT_SUMMARY.md` is not in the repository. It was moved to the desktop Trash at 13:54 on 2026-11-16, three minutes after the brief was saved; the copy in the Trash was read for the targets. Because it was removed by hand, it was not put back. The README carries the updated description instead, and the filled-in target tables are in the final report below.
- The summary and the brief speak of "the price lane of `/ask`". In the code `POST /ask` and `POST /ask/stream` only answer from documents; the price lane is reached through `GET /chat/ask`.

- The summary counts 189 tests; the suite had 189 at the start, so that matched. It says the chat page is 220 lines and 11 KB; it was 222 lines and 11 KB.
- The summary's "45 drugs" is reached by grouping labels on the first word of the generic name, which is what the code's name resolution does. It is 50 distinct generic names.
- 14 of the 50 labels are in the older format with no numbered sections (carbamazepine, buspirone, phenobarbital, ibuprofen and others). The summary does not say so; it matters because they have no section 17 and no 8.1 to quote.
- The README says the repricing constants are "stored as constants in `cli.py`"; they are in `desk.py`. Not changed (out of scope).
- FastAPI 0.141 no longer copies an included router's routes into `app.routes`. Nothing in the summary depends on it, but a test that only reads `app.routes` checks nothing; the route-table test walks the routers.

## For the owner

- **Nothing is committed.** Your standing instruction is that you make the commits, so everything is uncommitted on `main` (no branch was made, for the same reason). Your own seven edited files from before this run are still in the working tree beside mine: I did not touch `ask.py`, `test_ask.py` or `test_chat.py`, and in `README.md`, `chat.html`, `cli.py` and `serve.py` your edits are kept and mine are added.
- **A pharmacist's review**, before anything here is trusted: `tests/pairs_qa.jsonl` (18 pairs, each with its passage), `classes.json` (classes from openFDA, the aliases, and the hand-written terms for 6 drugs), and `TRIGGERS` in `review.py` (eGFR and creatinine clearance below 60, ALT and AST above 40, pregnancy). All are marked unreviewed and each entry names its source. Set `"reviewed": true` only after that.
- **The cache warm-up.** `pharmacli patients mock` then `pharmacli patients warm` with Chroma and Ollama up. On this laptop a pair took 4 s once the model was loaded (not the 75 s the summary measured for a free-text `interactions` answer); the 25 mock patients need a few hundred pairs, so expect tens of minutes. It carries on where it stopped.
- **The usability session**: the script is at the end of the README section "Patient profiles, pharmacist sign-in and the patient view". It needs real people.
- **The model.** With `qwen2.5:3b-instruct`, 10 of 14 pairs where a label says something ended in manual review. None was shown milder than the key, but the useful result is rare. A larger model (`PHARMACLI_LLM`) is the obvious lever; bump `PROMPT` in `review.py` and re-run `pharmacli docs eval --pairs` to compare.
- **After `pharmacli docs refresh`, restart the API** (or run `patients warm`): the API holds the corpus it booted with, so a refreshed label's pairs are re-checked only once it reloads.
- **Nothing came up from "Left to the owner"** other than the commits: no push, no history rewrite, `pharmacli.db`, the DVC pointer and the demo fixture untouched, no real person's data, no CI check loosened (one step added), and the full pair batch over all 990 pairs not run.

## Final report

### 1. Outcome

All six milestones are built, tested and checked end to end on synthetic data, and the
README describes the tool as it now is. The suite went from 189 to 282 tests and passes
on Python 3.12 and 3.10; lint is clean; mypy has the same nine errors it started with;
every page scores 100 for Lighthouse accessibility and fits 375 px; the Docker image
builds and serves every page. Nothing is committed: per your standing instruction the
work is uncommitted on `main`, beside your own seven edited files, for you to commit.
The weak point is the pair categories: with the 3B model most pairs where a label says
something end in "Needs manual review" (none is shown milder than the key, but few get
a category).

### 2. Targets

| Metric | Target | Result | Evidence |
|---|---|---|---|
| Text contrast | every text colour ≥ 4.5:1 | met: lowest text pair 4.79:1; borders 4.62:1 (3:1 needed) | `tests/test_theme.py` reads the tokens |
| Theme in one place | colours defined once | met: 9 tokens in `theme.css`, no hex colour elsewhere | same test |
| Page weight | chat under 30 KB, no external requests | met: chat 16.9 KB; sign-in 10.8, pharmacist 28.2, patient 13.6 KB (60 KB budget); no other-origin URL | same test |
| Phone layout | usable at 375 px | met: no sideways scroll on any page | headless Chrome, `scrollWidth 375` |
| Lighthouse accessibility | ≥ 90 | met: 100 on all four pages | Lighthouse 12.8.2 |
| Accounts | named pharmacists, salted hashes | met: scrypt, 16-byte salt, N=2^17 r=8 p=1 | `tests/test_patients.py` |
| Roles | checked on every patient endpoint | met | route-table test in `tests/test_auth.py` |
| Access-control tests | no token, expired, wrong role on every patient endpoint | met, plus malformed, signed out, no session, no role | generated from the route table |
| Audit trail | every profile read or change, who and request id | met: append-only `audit` table, allowed and denied | `tests/test_auth.py`, `tests/test_profiles.py` |
| Session length | one shift, with sign-out | met: 8 h, `POST /auth/logout`, enforced server-side | `tests/test_auth.py` |
| Mock patients | 25, fixed seed, every drug labeled | met | `tests/test_patients.py`; `pharmacli patients mock` |
| Upload validation | every rejected row with line and reason | met | `tests/test_patients.py`, `tests/test_profiles.py` |
| Indicator history | every update kept, nothing overwritten | met: append-only by trigger | `tests/test_patients.py` |
| Patient data in logs and `/metrics` | none, checked by a test | met | canary test in `tests/test_auth.py` |
| Category accuracy | ≥ 90% agreement with a pharmacist-labeled key | **not measured**: no pharmacist key exists. Provisional, on my unreviewed 18-pair key: 8 of 18 agree (4 of the 14 where a label says something) | `pharmacli docs eval --pairs`. To measure: have a pharmacist correct `tests/pairs_qa.jsonl`, then run that command |
| Missed severe interactions | none placed milder | met on the provisional key: 0 of 10 | same run |
| Cited recommendations | every one cites a label page; uncited withheld | met: every item carries its citation; what cannot be cited is "Needs manual review" | `tests/test_review.py` |
| Coverage | pairs checked out of total | met: "N of M pairs checked", pending and unchecked named | `tests/test_review.py` |
| Cached report, 6 drugs | under 1 minute | met here: 0.66–0.79 s | `pharmacli patients report ID` on one warmed mock patient. On your laptop: `patients warm`, then the same command prints its time |
| Pharmacist decisions | accepted or overridden, override rate tracked | built: every item, counts on the page and in `/metrics` → `decisions`. Rate in real use **not measured**: needs real use | `tests/test_review.py` |
| Time to add a patient | under 2 minutes | **not measured**: needs people. Recorded per save in `/metrics` → `new_patient.seconds` | usability script in the README |
| Valid on first submit | ≥ 90% | **not measured**: `/metrics` → `new_patient.first_submit_valid` / `first_submit_invalid` | same |
| Duplicate patients | flagged before saving, every test case | met: case, spacing and accent variants, checked by the server | `tests/test_entry.py` |
| Unmatched drug names | flagged at entry, none unflagged in a report | met | `tests/test_entry.py`; keyboard run in headless Chrome |
| Keyboard-only entry | the whole form | met | headless Chrome, key events only |
| Records a patient can see | only their own | met: no id in their routes; another patient's id answers like a missing one | `tests/test_patient_view.py` |
| Patient-specific questions refused | all, on a key extended with patient phrasings | met: 23 of 23 by the gate; the chat's 5 of 5 unchanged | `pharmacli docs eval --retrieval-only` |
| Task completion | ≥ 90% in a usability test | **not measured**: needs people | usability script in the README |
| Counseling text | section 17 word for word | met: the `docs handout` function, per drug | `tests/test_patient_view.py` |

### 3. What changed

`git diff --stat 9d94feb` (this includes your own uncommitted edits to `README.md`,
`ask.py`, `chat.html`, `cli.py`, `serve.py`, `test_ask.py` and `test_chat.py`, which
were there before I started):

```text
 .dockerignore, .dvcignore, .gitignore      ignore patients.db everywhere
 .env.example, src/pharmacli/config.py      5 new settings
 .github/workflows/ci.yml                   the image check requests every page, stylesheet and script
 Dockerfile, docker-compose.yml             classes.json in the image; a patient-data volume
 pyproject.toml                             *.css and *.js as package data
 README.md                                  +360: the new features, routes, data, limits, measures, usability script
 src/pharmacli/chat.html                    restyled on the tokens; labels, landmarks, aria-busy while streaming
 src/pharmacli/serve.py                     +600: static files, sign-in, role guards, audit, patient routes, worker
 src/pharmacli/cli.py, parser.py            patients account/mock/list/report/warm; docs classes; docs eval --pairs; interactions --category
 src/pharmacli/docs.py, enrich.py           pharmacologic classes from openFDA, kept in classes.json
 src/pharmacli/rag.py                       join() and page_at() made public for quoting by page
 src/pharmacli/rageval.py                   pair-key scoring; patient-phrased rows scored against the gate
 tests/conftest.py, loadtest.py, rag_qa.jsonl   fixtures; the load test skips patient rows; 23 refusal rows
```

New files: `theme.css` (milestone 1); `patients.py`, `login.html`, `app.js`,
`tests/test_patients.py`, `tests/test_auth.py`, `tests/test_theme.py` (1–2);
`pharmacist.html`, `pharmacist.js`, `tests/test_profiles.py` (3); `review.py`,
`classes.json`, `tests/pairs_qa.jsonl`, `tests/test_review.py` (4);
`tests/test_entry.py` (5); `patient.html`, `patient.js`, `tests/test_patient_view.py` (6).

New commands: `patients account|mock|list|report|warm`, `docs classes`,
`docs eval --pairs`, `interactions --category`. New routes: `/login`, `/pharmacist`,
`/patient`, `/static/{name}`, `/auth/login|logout|password`, `/patients…` (pharmacist)
and `/patients/me…` (patient), listed in the README. New settings:
`PHARMACLI_PATIENT_DB`, `PHARMACLI_SCRYPT_N`, `PHARMACLI_SESSION_HOURS`,
`PHARMACLI_LOCK_AFTER`, `PHARMACLI_LOCK_MINUTES`. New tables, in `patients.db` only:
`patient`, `drug`, `reading`, `account`, `session`, `attempt`, `audit`, `pair`,
`decision`, `question`.

Behaviour of existing routes that changed on purpose: `/predict`, `/ask`, `/ask/stream`
and `/chat/*` refuse a patient token (403); `/metrics` keys an unmatched path as
`(no route)` instead of the URL, and gained `decisions` and `new_patient`; a signed-in
token whose session ended is refused everywhere. Tokens from `pharmacli token` behave
exactly as before.

### 4. Decisions and differences

Under "Decisions" and "Differences from the summary" above.

### 5. Existing tests I changed

None. `tests/conftest.py` gained fixtures, and its stand-in Ollama now also accepts a
function as its reply; every existing use is unchanged. `tests/loadtest.py` (not part of
the suite) skips the new patient-phrased rows so the load stays the 50 chat questions.

### 6. For the owner

Under "For the owner" above: the commits, a pharmacist's review of the pair key, class
terms and triggers, the cache warm-up, the usability session, the model's coverage, and
restarting the API after `docs refresh`.

### 7. Noticed, not changed

- mypy's nine errors in `rules/__init__.py`, `claims.py`, `track.py`, `nn.py`, `nlp.py`.
- Starlette warns that its test client's use of `httpx` is deprecated.
- The README places the repricing constants in `cli.py`; they are in `desk.py`.
- Text from PDF tables comes out flattened, so a quote from a table can carry a word from the next cell. The page number is the check.
- `docs handout` uses the first of two labels for a drug that has two (potassium chloride, atorvastatin, pregabalin, rosuvastatin, venlafaxine).
- 14 older-format labels have no section 17, so their counseling text is "ask your pharmacist".
- Repeated wrong passwords lock a name, which lets anyone lock a known pharmacist out for 15 minutes. Usual trade-off; per-address limits are the next step.
- Forwarded patient questions can be read but not marked answered.
- The API keeps the corpus it booted with; `docs refresh` in another process takes effect at the next restart.

### 8. Try it

From a clean clone, with Docker for the model:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[serve,rag]"
echo "PHARMACLI_JWT_SECRET=$(openssl rand -hex 32)" >> .env
docker compose up -d --wait chroma ollama           # first run pulls ~2 GB
pharmacli docs fetch                                # the 56 PDFs
pharmacli docs ingest                               # for label questions
pharmacli patients account alice                    # type a 12+ character password
pharmacli patients mock                             # 25 synthetic patients
pharmacli patients list                             # note an id
pharmacli patients warm                             # tens of minutes; stop and rerun any time
pharmacli patients report <id>                      # the report as text, and how long it took
uvicorn pharmacli.serve:app --port 8000
```

Pharmacist: open <http://127.0.0.1:8000/login>, sign in as `alice`, pick a patient;
the report is under the profile. Patient: on that profile open "Add a reading, see the
history, or give the patient a login", create a login, copy the one-time password, sign
out, sign in with it, choose a new password, and the patient's own page opens.
