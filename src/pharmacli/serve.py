# Week 6: the prediction API. Week 10: the same app, in production dress --
# JWT on the data endpoints, the week 9 RAG assistant behind /ask (streaming
# over SSE), one JSON log line per request with a request id, and /metrics.
#
# One app, not two: /predict and /ask share the config, the logging and the
# auth, and a second FastAPI process would have duplicated all three.

import asyncio
import base64
import binascii
import collections
import contextvars
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager, closing, contextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from pharmacli import docs, patients, rag, review
from pharmacli.config import load as load_config

log = logging.getLogger(__name__)

# module-level state, filled once by lifespan() at boot
S: dict = {}

# the request id of the request being handled, so a log line written five
# frames deep carries it without every function taking an id argument
REQ_ID: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

# ponytail: in-process counters, lost on restart. That is the right size for
# one container; point a Prometheus at it and these become a text exposition
# on /metrics instead.
M: dict = {"requests": collections.Counter(), "errors": collections.Counter(),
           "tokens": collections.Counter(),
           # the new-patient form, anonymously: whether the first submit was
           # valid, and how long entry took from first keystroke to save
           "entry": collections.Counter(), "entry_s": collections.deque(maxlen=500),
           "latency": collections.defaultdict(lambda: collections.deque(maxlen=500)),
           "stages": collections.defaultdict(lambda: collections.deque(maxlen=500))}


# ── structured logging ───────────────────────────────────────────────────────
class JsonLines(logging.Formatter):
    # one JSON object per line: greppable by hand, parseable by anything that
    # collects container stdout. Fields passed as extra={"fields": {...}} are
    # merged in, so a request line and a model line share one shape.
    def format(self, r: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(r, "%Y-%m-%dT%H:%M:%S"), "level": r.levelname,
               "logger": r.name, "request_id": REQ_ID.get(), "msg": r.getMessage()}
        out |= getattr(r, "fields", {})
        if r.exc_info:
            out["exc"] = self.formatException(r.exc_info)
        return json.dumps(out)


def _setup_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLines())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # our middleware logs every request with timing and a request id; uvicorn's
    # access line would be the same event again, unstructured
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("httpx").setLevel(logging.WARNING)


# ── auth: HS256 JWT on nine lines of stdlib ──────────────────────────────────
# No PyJWT: this verifies one algorithm, which is the whole of what a service
# with a shared secret needs. The two ways a hand-rolled verifier goes wrong
# are both closed below -- the algorithm is required to be HS256 (so a token
# claiming "none" is rejected rather than trusted) and the signature is
# compared with hmac.compare_digest. tests/test_api.py holds both to it.
ALG = {"alg": "HS256", "typ": "JWT"}
TTL = 3600


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _secret() -> str:
    s = load_config().jwt_secret
    if not s:
        # Missing secret is not a reason to run without auth. A per-process
        # random one keeps the endpoints closed; tokens minted by any other
        # process (`pharmacli token` in another shell, a second worker) will
        # not verify, which is the loud way to be misconfigured.
        s = os.environ.setdefault("PHARMACLI_JWT_SECRET", secrets.token_urlsafe(32))
        log.warning("PHARMACLI_JWT_SECRET is not set: generated a temporary one, so "
                    "tokens issued by any other process will be rejected")
    return s


def issue(sub: str, ttl: int = TTL, **claims) -> str:
    # a sign-in adds a role, the id of its server-side session and, for a
    # patient, the id of their own record; `pharmacli token` adds nothing
    now = int(time.time())
    payload = {"sub": sub, "iat": now, "exp": now + ttl, **claims}
    body = f"{_b64(json.dumps(ALG).encode())}.{_b64(json.dumps(payload).encode())}"
    sig = hmac.new(_secret().encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}"


def verify(token: str) -> dict:
    try:
        head, payload, sig = token.split(".")
        # signature first: nothing attacker-shaped is parsed before it verifies
        want = hmac.new(_secret().encode(), f"{head}.{payload}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(want, _unb64(sig)):
            raise ValueError("bad signature")
        if json.loads(_unb64(head)).get("alg") != ALG["alg"]:
            raise ValueError("unsupported algorithm")
        claims = json.loads(_unb64(payload))
        expired = float(claims.get("exp", 0)) < time.time()
    # AttributeError: a header or payload that is valid JSON but not an object
    except (ValueError, KeyError, TypeError, AttributeError, binascii.Error) as exc:
        raise HTTPException(401, f"invalid token: {exc}") from exc
    if expired:
        raise HTTPException(401, "token expired")
    return claims


bearer = HTTPBearer(auto_error=False,
                    description="a token from `pharmacli token`, or from signing in at /login")


def _session(claims: dict) -> dict:
    # A signed-in token names a session the server holds, which is what lets
    # sign-out, expiry and a password reset end it before its `exp`. The role,
    # the patient record and the forced password change come from that record,
    # not from the token. A token from `pharmacli token` has neither field and
    # stands on its signature alone, exactly as before.
    if "jti" not in claims and "role" not in claims:
        return claims
    with closing(patients.connect(load_config().patient_db)) as conn:
        acct = patients.session(conn, str(claims.get("jti", "")))
    if acct is None:
        raise HTTPException(401, "signed out or expired -- sign in again")
    return claims | {"role": acct["role"], "pid": acct["patient"],
                     "must_change": bool(acct["must_change"])}


def caller(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    # auto_error=False only to answer a missing header with 401 rather than
    # HTTPBearer's 403: nothing was forbidden, nothing was presented
    if cred is None:
        raise HTTPException(401, "this endpoint needs a bearer token -- run `pharmacli token`")
    claims = _session(verify(cred.credentials))
    # Every route this guards predates patient accounts: prices, costs and
    # inventory, and the model's own wording. A patient sees their own records
    # and quoted label text and nothing else, so none of them is open to one.
    if claims.get("role") == "patient":
        raise HTTPException(403, "this is not open to a patient account")
    return str(claims.get("sub", "?"))


# ── one guard per role, on every route under one prefix ─────────────────────
# A patient route is any route that reads or writes patient data. All of them
# live under PREFIX on one of the two routers below, so the role check is a
# dependency of the router and cannot be forgotten on a new route
# (tests/test_auth.py walks the route table to hold that). The same guard
# writes the audit row, allowed or denied, so no handler has to remember to.
PREFIX = "/patients"


def _guard(request: Request, cred: HTTPAuthorizationCredentials | None, role: str) -> dict:
    who, got, claims, denied = "-", "-", {}, None
    try:
        if cred is None:
            raise HTTPException(401, "sign in first")
        claims = verify(cred.credentials)
        who = str(claims.get("sub", "?"))
        claims = _session(claims)
        got = claims.get("role") or "-"
        if got != role:
            # a token with no role (from `pharmacli token`) lands here too
            raise HTTPException(403, f"this needs a {role} account")
        if claims["must_change"]:
            raise HTTPException(403, "choose your own password first")
    except HTTPException as exc:
        denied = exc
    # which record: the one in the URL for a pharmacist, their own for a patient
    pid = request.path_params.get("pid") or (claims.get("pid") if not denied else None)
    with closing(patients.connect(load_config().patient_db)) as conn:
        patients.audit(conn, who, got, f"{request.method} {request.scope['route'].path}",
                       denied is None, REQ_ID.get(), pid)
    if denied:
        raise denied
    return claims


def pharmacist(request: Request,
               cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
    return _guard(request, cred, "pharmacist")


def patient(request: Request,
            cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
    return _guard(request, cred, "patient")


staff = APIRouter(prefix=PREFIX, dependencies=[Depends(pharmacist)])
own = APIRouter(prefix=f"{PREFIX}/me", dependencies=[Depends(patient)])


# ── boot ─────────────────────────────────────────────────────────────────────
def _load_model(cfg) -> None:
    # pandas and sklearn are the [analysis] extra, and only /predict needs them.
    # Imported here so `pharmacli chat` and the document endpoints run without.
    from pharmacli import model
    from pharmacli.frame import load as load_frame

    path = Path(cfg.model_path)
    if not path.exists():
        raise FileNotFoundError(f"no promoted model at {path} -- run `pharmacli sweep`")

    pipe, card = model.load_promoted(str(path))

    # Builds features over the FULL panel at boot (~8s since _cycle_features
    # was vectorized, was ~28s). Do NOT slice to a shorter window to speed it
    # up: weeks_since_move is a cumsum from each NDC's first row, so truncating
    # saturates it rather than shortening it. Measured at a 26-week window, 478
    # NDCs come out wrong -- and it is the STALEST drugs that break, which are
    # exactly the ones the feature exists to flag. It also only saved 5%,
    # because the cost was never row count.
    log.info("building panel from %s", cfg.db)
    everything = model.features(load_frame(cfg.db), labeled_only=False)
    # the newest week is unlabelled by definition -- its outcome has not
    # happened yet -- which is exactly the week worth scoring
    newest = everything["as_of_date"].max()
    live = everything[everything["as_of_date"] == newest].set_index("ndc")

    S.update(pipe=pipe, card=card, live=live[~live.index.duplicated()],
             week=str(newest.date()), version=f"{card['name']}/{card['version']}")
    log.info("serving %s over %d NDCs from %s", S["version"], len(S["live"]), S["week"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    _setup_logging(cfg.log_level)
    S["boot"] = time.monotonic()

    # A missing model or panel used to be fatal. In a container it is the
    # normal first state -- both files are gitignored and neither is in the
    # image -- and it stops nothing but /predict, so it is reported by /health
    # instead of refusing to start.
    try:
        _load_model(cfg)
    except Exception as exc:                  # joblib, pandas and sklearn all raise their own
        S["model_error"] = f"{type(exc).__name__}: {exc}"
        log.warning("/predict is off: %s", S["model_error"])

    async with AsyncExitStack() as stack:
        worker = None
        try:
            S["rag"] = await stack.enter_async_context(rag.session(cfg))
            log.info("documents from %s, model %s", cfg.chroma_url, cfg.llm)
            # set at boot too, so pairs a restart interrupted are picked up again
            S["wake"] = asyncio.Event()
            S["wake"].set()
            worker = asyncio.create_task(_check_pairs())
        except (OSError, ValueError) as exc:  # no corpus.json, or an unreadable one
            S["rag_error"] = f"{type(exc).__name__}: {exc}"
            log.warning("/ask is off: %s", S["rag_error"])
        yield
        if worker:
            worker.cancel()
    S.clear()


async def _check_pairs() -> None:
    # The queue's one worker. It sleeps until a patient's drugs change, then
    # asks the model about each pair no patient has an answer for yet, one at
    # a time, standing aside whenever a pharmacist is waiting on a live answer.
    # Nothing about a patient is in what it sends or logs: a pair is two labels.
    wake = S["wake"]
    while True:
        await wake.wait()
        wake.clear()
        cfg = load_config()
        if not Path(cfg.patient_db).exists():     # never create the file just to look in it
            continue
        try:
            done = await review.work(S["rag"], cfg.patient_db,
                                     review.Labels(S["rag"].corpus, docs.load_classes()),
                                     busy=lambda: S.get("live", 0) > 0)
            if done:
                log.info("pair checks", extra={"fields": {"checked": done}})
        except Exception as exc:                  # the model is down, a PDF is unreadable...
            log.warning("pair checks paused until the next change: %s: %s",
                        type(exc).__name__, exc)


@contextmanager
def _live():
    # a pharmacist is waiting on an answer: queued pair checks hold off
    S["live"] = S.get("live", 0) + 1
    try:
        yield
    finally:
        S["live"] = S.get("live", 1) - 1


app = FastAPI(title="pharmacli", version="10.0", lifespan=lifespan,
              description="Price-move predictions, and answers from package inserts "
                          "and reimbursement documents with the page each came from.")

# No CORS middleware. The only browser client was the file:// buying desk, whose
# "null" origin needed allow_origins=["*"]; with the desk gone that would widen
# the boundary for nobody. Add it back the day a real web client exists, scoped
# to that origin rather than "*".


# ── one JSON line per request ────────────────────────────────────────────────
@app.middleware("http")
async def observe(request: Request, call_next):
    # capped: a caller-supplied id is echoed into every log line and header
    rid = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex[:12]
    REQ_ID.set(rid)
    t = time.perf_counter()

    def path() -> str:
        # The route template ("/patients/{pid}"), never the URL: a patient id
        # must not become a metric label or a log field. A path that matched no
        # route has no template; it is logged as sent unless it sits under the
        # patient prefix, where a mistyped URL could still carry an id.
        route = getattr(request.scope.get("route"), "path", None)
        if route:
            return route
        return f"{PREFIX}/(no route)" if request.url.path.startswith(PREFIX) else request.url.path

    try:
        response = await call_next(request)
    except Exception:
        M["errors"]["unhandled"] += 1
        log.exception("unhandled error", extra={"fields": {"path": path()}})
        raise
    ms = round((time.perf_counter() - t) * 1000, 1)
    # For /ask/stream this is time to the first byte: the body is still
    # generating when the middleware returns. Unmatched paths share one key, so
    # the metrics cannot grow a key per distinct URL.
    route = getattr(request.scope.get("route"), "path", "(no route)")
    key = f"{request.method} {route}"
    M["requests"][key] += 1
    M["latency"][key].append(ms)
    if response.status_code >= 400:
        M["errors"][f"{key} {response.status_code}"] += 1
    response.headers["x-request-id"] = rid
    response.headers["x-response-time-ms"] = str(ms)
    log.info("request", extra={"fields": {"method": request.method, "path": path(),
                                          "status": response.status_code, "ms": ms}})
    return response


@app.exception_handler(HTTPException)
async def with_request_id(request: Request, exc: HTTPException):
    # FastAPI's own shape plus the id, so an error a user reports can be found
    # in the logs without asking them what time it was
    return JSONResponse({"detail": exc.detail, "request_id": REQ_ID.get()},
                        status_code=exc.status_code, headers=exc.headers)


# ── dependencies ─────────────────────────────────────────────────────────────
def get_rag() -> rag.Rag:
    if not S.get("rag"):
        raise HTTPException(503, f"the document service is not available: "
                                 f"{S.get('rag_error', 'not started')}")
    return S["rag"]


def get_corpus() -> dict:
    # the document list: the one the document session loaded, or read from disk
    # when that session is not up -- profiles need labels' names, not the index
    if S.get("rag"):
        return S["rag"].corpus
    try:
        return docs.load_corpus()
    except (OSError, ValueError) as exc:
        raise HTTPException(503, f"no corpus.json to match drug names against: {exc}") from exc


def get_labels(corpus: dict = Depends(get_corpus)) -> review.Labels:
    # what a pair check and a report read: the labels, the class terms
    # (classes.json) and the folder the label PDFs are in
    return review.Labels(corpus, docs.load_classes())


def _record(timings: dict, tokens: dict, who: str, refused: str | None) -> None:
    for stage, ms in timings.items():
        M["stages"][stage].append(ms)
    M["tokens"].update(tokens)
    # the answer's own line: who asked, what it cost, and whether it refused.
    # The request line the middleware writes carries the same request_id.
    log.info("answered", extra={"fields": {"sub": who, "refused": refused,
                                           "completion_tokens": tokens.get("completion", 0),
                                           **{f"{k}_ms": v for k, v in timings.items()}}})


# ── request / response ───────────────────────────────────────────────────────
class Overrides(BaseModel):
    unit_price: float | None = Field(None, gt=0, le=100_000,
                                     description="dollars per unit; must be positive")
    age_days: float | None = Field(None, ge=0, le=3650,
                                   description="days since the price took effect")


class PredictIn(BaseModel):
    # NADAC NDCs are 11 digits, zero-padded. Anything else is a typo, and
    # FastAPI rejects it with a field-level 422 before any of our code runs.
    ndc: str = Field(..., pattern=r"^\d{11}$", examples=["00093721410"])
    overrides: Overrides | None = None


class AskIn(BaseModel):
    question: str = Field(..., min_length=3, max_length=500,
                          examples=["what are the boxed warnings on carbamazepine"])
    k: int | None = Field(None, ge=1, le=10, description="passages to retrieve")
    chunking: Literal["fixed", "section"] | None = None


# ── open endpoints: liveness and numbers, no token ───────────────────────────
@app.get("/health")
def health():
    # open on purpose: the compose healthcheck and any load balancer call it
    # before anyone has a token, and it says nothing a caller does not know
    out: dict = {"status": "ok", "version": app.version,
                 "uptime_s": round(time.monotonic() - S.get("boot", 0), 1)}
    if S.get("card"):
        c = S["card"]
        out |= {"model": S["version"], "trained_at": c["trained_at"],
                "test_pr_auc": c["test_pr_auc"], "threshold": c["threshold"],
                "data_md5": c["data_md5"], "week": S["week"], "ndcs": len(S["live"])}
    else:
        out |= {"model": None, "model_error": S.get("model_error")}
    out["documents"] = "ready" if S.get("rag") else S.get("rag_error", "not started")
    return out


def _pct(values, q: float):
    v = sorted(values)
    return v[min(len(v) - 1, int(q * len(v)))] if v else None


@app.get("/metrics")
def metrics():
    # JSON, not the Prometheus text format: nothing scrapes this yet, and a
    # human reading `curl /metrics` during a load test is the actual consumer.
    # The stage block is the one that matters -- it says which of embed,
    # retrieve, prefill or decode is eating the answer's latency.
    return {"uptime_s": round(time.monotonic() - S.get("boot", 0), 1),
            "requests": dict(M["requests"]), "errors": dict(M["errors"]),
            "tokens": dict(M["tokens"]),
            "latency_ms": {k: {"n": len(v), "p50": _pct(v, 0.5), "p95": _pct(v, 0.95),
                               "max": max(v)} for k, v in M["latency"].items()},
            "stage_ms": {k: {"n": len(v), "p50": _pct(v, 0.5), "p95": _pct(v, 0.95)}
                         for k, v in M["stages"].items()},
            # how often a pharmacist overrode what the labels suggested: counts only
            "decisions": _decision_counts(),
            # the two targets for entering a patient by hand, as the page reports them
            "new_patient": dict(M["entry"]) | {
                "seconds": {"n": len(M["entry_s"]), "p50": _pct(M["entry_s"], 0.5),
                            "p95": _pct(M["entry_s"], 0.95)}}}


def _decision_counts() -> dict:
    path = load_config().patient_db
    if not Path(path).exists():                   # reading the metrics creates nothing
        return {"accepted": 0, "overridden": 0}
    with closing(patients.connect(path)) as conn:
        return patients.decision_counts(conn)


# ── behind a token ───────────────────────────────────────────────────────────
@app.post("/predict")
def predict(req: PredictIn, who: str = Depends(caller)):
    import numpy as np
    import pandas as pd

    from pharmacli import model

    if not S.get("card"):
        raise HTTPException(503, f"no model is loaded: {S.get('model_error', 'not started')}")
    # Layer 2: is this a thing we know about?
    if req.ndc not in S["live"].index:
        raise HTTPException(404, f"NDC {req.ndc} is not in the {S['week']} NADAC "
                                 f"file. Run `pharmacli fetch` for a newer file, "
                                 f"or check the NDC.")
    row = S["live"].loc[req.ndc].copy()

    if req.overrides:
        for k, v in req.overrides.model_dump(exclude_none=True).items():
            row[k] = v
        # log_price is DERIVED from unit_price -- overriding one without the
        # other would feed the model a contradiction it cannot see.
        if req.overrides.unit_price is not None:
            row["log_price"] = float(np.log10(max(req.overrides.unit_price, 1e-6)))

    # Layer 3: is this row inside the world the model was trained on? A model
    # asked about a price 3,000 days old will answer confidently and be wrong;
    # refusing is more useful than a number nobody can act on.
    for col, (lo, hi) in S["card"]["bounds"].items():
        v = row.get(col)
        if v is not None and pd.notna(v) and not (lo <= float(v) <= hi):
            raise HTTPException(422, f"{col}={float(v):.4g} is outside the range this "
                                     f"model was trained on [{lo:.4g}, {hi:.4g}]. "
                                     f"Refusing rather than guessing.")

    # Unseen categoricals are warned about, not rejected: handle_unknown="ignore"
    # zeroes them, so the answer is valid, just built on less information.
    warnings = [f"{c}={row[c]!r} was not seen in training; prediction uses the "
                f"numeric features only for this field"
                for c, seen in S["card"]["categories"].items()
                if pd.notna(row.get(c)) and str(row[c]) not in seen]

    X = pd.DataFrame([row])[model.NUMERIC + model.CATEGORICAL]
    p = float(S["pipe"].predict_proba(X)[0, 1])
    thr = S["card"]["threshold"]
    return {
        "ndc": req.ndc, "description": str(row["description"]),
        "p_moves": round(p, 4), "threshold": round(thr, 4),
        "verdict": "buy ahead" if p >= thr else "hold",
        "unit_price": round(float(row["unit_price"]), 5),
        "age_days": None if pd.isna(row["age_days"]) else int(row["age_days"]),
        "as_of": S["week"], "model": S["version"], "warnings": warnings,
    }


@app.post("/ask")
async def ask(req: AskIn, who: str = Depends(caller), r: rag.Rag = Depends(get_rag)):
    # the whole answer in one JSON object: refusals, the passages behind it and
    # the per-stage timings. /ask/stream is the same events as they happen.
    with _live():
        out = await rag.collect(r, req.question, k=req.k, chunking=req.chunking)
    _record(out["timings_ms"], out["tokens"], who, out["reason"] or out["error"])
    return out


@app.post("/ask/stream")
async def ask_stream(req: AskIn, who: str = Depends(caller), r: rag.Rag = Depends(get_rag)):
    async def events():
        refused = None
        with _live():
            async for ev in rag.stream_answer(r, req.question, k=req.k, chunking=req.chunking):
                if ev["type"] in ("refusal", "error"):
                    refused = ev.get("reason") or ev.get("code")
                if ev["type"] == "done":
                    _record(ev["timings_ms"], ev["tokens"], who, refused)
                yield f"data: {json.dumps(ev)}\n\n"

    # server-sent events, not a websocket: the traffic is one-way and every
    # browser and curl already speaks it. The middleware puts the request id on
    # the response; only the body is late.
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"cache-control": "no-store"})


# ── the chat page, served by the same app ───────────────────────────────────
# Was chat.py: 142 lines of http.server with its own routing, its own threading
# server and its own JSON writer, next to this one. The page is unchanged; only
# what serves it is. `pharmacli chat` now runs uvicorn on 127.0.0.1.

CHAT_PAGE = Path(__file__).with_name("chat.html").read_text(encoding="utf-8")

# What the pages link. Read at import, like the page above: a file left out of
# the package data works from a checkout and is missing from the built image,
# and this way that fails the boot (and CI's image check) instead of a request.
ASSETS = {name: (kind, Path(__file__).with_name(name).read_bytes()) for name, kind in {
    "theme.css": "text/css", "app.js": "text/javascript", "pharmacist.js": "text/javascript",
    "patient.js": "text/javascript"}.items()}
# the patient upload's template, built from the same column list its reader uses
ASSETS["template.csv"] = ("text/csv", patients.template().encode())


@app.get("/static/{name}")
def static(name: str):
    # a fixed table, not a directory listing: nothing but these files is served
    if name not in ASSETS:
        raise HTTPException(404, "no such file")
    kind, body = ASSETS[name]
    return Response(body, media_type=kind)

# ponytail: one upload slot, because `chat` is one local person looking at their
# own shelf. Add a session id the day it serves more than one.
UPLOAD: dict = {}
MAX_UPLOAD = 5 * 1024 * 1024          # a 5,000-line inventory CSV is ~200KB


async def _capped(request: Request) -> bytes | None:
    # The request body, or None when it is over MAX_UPLOAD. The length header
    # first, so an oversized file is refused before it is read; then counted as
    # it streams, because a chunked body has no content-length to check. Shared
    # by the inventory upload and the patient upload.
    declared = request.headers.get("content-length") or "0"
    if not declared.isdigit() or int(declared) > MAX_UPLOAD:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_UPLOAD:
            return None
    return bytes(body)


@app.get("/chat", response_class=HTMLResponse)
def chat_page():
    # Open, because a browser navigating to a page cannot send a header. The
    # page is a shell: every endpoint below it needs the token, which reaches
    # it in the URL fragment -- the one part of a URL browsers never send.
    return CHAT_PAGE


@app.get("/chat/prompts")
def chat_prompts(who: str = Depends(caller)):
    from pharmacli import ask

    return [ask.chip(label, schema) for label, _, schema in ask.QUESTIONS]


@app.get("/chat/ask")
def chat_ask(q: str = Query(..., max_length=500), who: str = Depends(caller)):
    from pharmacli import ask

    # a label answer takes a minute on a CPU: the page streams it from
    # /ask/stream rather than staring at "..." while this request waits
    if S.get("rag") and ask.lane(q) == "documents":
        return {"label": "documents", "stream": True, "chips": ask.near(ask.match(q))}
    held = UPLOAD.get("held")
    label, lines, chips = ask.answer(q, db=load_config().db, shelf=held and held["shelf"])
    text = "\n".join(lines)
    if held and label:
        text = f"from {held['name']} (not saved)\n\n{text}"
    return {"label": label, "text": text, "chips": chips}


@app.post("/chat/upload")
async def chat_upload(request: Request, who: str = Depends(caller)):
    import tempfile

    from pharmacli import ask
    from pharmacli.cli import read_inventory

    name = (request.headers.get("x-filename") or "inventory.csv")[:200]
    empty = {"name": name, "lines": 0, "skipped": 0, "reasons": [], "suggest": []}
    body = await _capped(request)
    if body is None:
        return empty | {"error": f"file is larger than {MAX_UPLOAD // 2**20} MB"}
    with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as fh:
        path = fh.name
        fh.write(body)
    try:
        rows, skipped = read_inventory(path)
    except OSError as exc:
        return empty | {"error": f"cannot read the file: {exc}"}
    finally:
        os.remove(path)

    reasons = [f"line {line}: {why}" for line, why in skipped[:5]]
    if not rows:
        UPLOAD.pop("held", None)
        return empty | {"skipped": len(skipped), "reasons": reasons,
                        "error": "no usable ndc,cost rows"}
    shelf = ({n: c for n, c, _, _ in rows}, {n: (oh, mo) for n, _, oh, mo in rows})
    UPLOAD["held"] = {"name": name, "rows": rows, "shelf": shelf}
    return {"name": name, "lines": len(rows), "skipped": len(skipped), "reasons": reasons,
            "suggest": ask.suggestions(load_config().db, shelf)}


@app.post("/chat/save")
def chat_save(who: str = Depends(caller)):
    from pharmacli.store import connect, watch_cost

    held = UPLOAD.get("held")
    if not held:
        return {"error": "nothing attached"}
    with closing(connect(load_config().db)) as conn:
        changed = watch_cost(conn, held["rows"])
    UPLOAD.pop("held")          # the shelf holds it now; answer from there
    return {"saved": len(held["rows"]), "changed": changed}


@app.post("/chat/clear")
def chat_clear(who: str = Depends(caller)):
    UPLOAD.pop("held", None)
    return {"ok": True}


# ── signing in ───────────────────────────────────────────────────────────────
# Accounts, sessions and the audit trail live in the patient database
# (patients.py). There is no default account: `pharmacli patients account NAME`
# creates a pharmacist, and a pharmacist creates each patient's login.

class SignIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=1024)


class NewPassword(BaseModel):
    current: str = Field(..., min_length=1, max_length=1024)
    new: str = Field(..., min_length=1, max_length=1024)


def _db():
    return closing(patients.connect(load_config().patient_db))


@app.post("/auth/login")
def sign_in(req: SignIn):
    cfg = load_config()
    with _db() as conn:
        try:
            acct = patients.sign_in(conn, req.username, req.password, cfg.scrypt_n,
                                    cfg.lock_after, cfg.lock_minutes * 60)
        except patients.Locked as exc:
            patients.audit(conn, "-", "-", "sign-in", False, REQ_ID.get())
            raise HTTPException(429, "too many wrong passwords -- try again in "
                                     f"{math.ceil(exc.seconds / 60)} minutes") from exc
        if acct is None:
            # the name typed is not recorded: a password typed into the wrong
            # box would otherwise sit in the audit table
            patients.audit(conn, "-", "-", "sign-in", False, REQ_ID.get())
            # one answer for an unknown name and for a wrong password
            raise HTTPException(401, "wrong username or password")
        ttl = int(cfg.session_hours * 3600)
        jti = patients.open_session(conn, acct["id"], ttl)
        patients.audit(conn, acct["username"], acct["role"], "sign-in", True, REQ_ID.get(),
                       acct["patient"])
    own_record = {"pid": acct["patient"]} if acct["patient"] else {}
    return {"token": issue(acct["username"], ttl, role=acct["role"], jti=jti, **own_record),
            "role": acct["role"], "must_change": bool(acct["must_change"])}


def _signed_in(cred: HTTPAuthorizationCredentials | None) -> dict:
    if cred is None:
        raise HTTPException(401, "sign in first")
    claims = _session(verify(cred.credentials))
    if "jti" not in claims:
        raise HTTPException(403, "this token did not come from signing in")
    return claims


@app.post("/auth/logout")
def sign_out(cred: HTTPAuthorizationCredentials | None = Depends(bearer)):
    claims = _signed_in(cred)
    with _db() as conn:
        patients.close_session(conn, claims["jti"])
        patients.audit(conn, claims["sub"], claims["role"], "sign-out", True, REQ_ID.get(),
                       claims["pid"])
    return {"ok": True}


@app.post("/auth/password")
def change_password(req: NewPassword,
                    cred: HTTPAuthorizationCredentials | None = Depends(bearer)):
    claims = _signed_in(cred)
    with _db() as conn:
        try:
            patients.change_password(conn, claims["sub"], req.current, req.new,
                                     load_config().scrypt_n)
        except ValueError as exc:
            patients.audit(conn, claims["sub"], claims["role"], "password change", False,
                           REQ_ID.get(), claims["pid"])
            raise HTTPException(422, str(exc)) from exc
        patients.audit(conn, claims["sub"], claims["role"], "password change", True,
                       REQ_ID.get(), claims["pid"])
    return {"ok": True}


# ── the signed-in pages ──────────────────────────────────────────────────────
# Open shells, like /chat: a browser navigating to a page cannot send a header,
# and there is no data in them. Unlike the chat they carry no inline script or
# style, so this policy can refuse everything that is not from this origin.
CSP = {"Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'"}
PAGES = {name: Path(__file__).with_name(f"{name}.html").read_text(encoding="utf-8")
         for name in ("login", "pharmacist", "patient")}


def _page(name: str):
    def page() -> HTMLResponse:
        return HTMLResponse(PAGES[name], headers=CSP)
    return page


for _name in PAGES:
    app.get(f"/{_name}", include_in_schema=False)(_page(_name))


# ── patient routes: pharmacists ──────────────────────────────────────────────

def _wake() -> None:
    # new drugs mean new pairs to check: nudge the queue, and return without waiting
    if S.get("wake"):
        S["wake"].set()


def _profile(conn, pid: str, corpus: dict) -> dict:
    found = patients.profile(conn, pid)
    if found is None:
        raise HTTPException(404, "no such patient")
    return found | {"drugs": review.tag(found["drugs"], corpus)}


def _stored(rows: list, rejected: list, by: str) -> dict:
    with _db() as conn:
        result = patients.load(conn, rows, by)
    _wake()
    return {"created": len(result["created"]), "updated": len(result["updated"]),
            "rejected": [{"line": line, "reason": why}
                         for line, why in sorted(rejected + result["rejected"])]}


@staff.get("")
def patient_list(corpus: dict = Depends(get_corpus)):
    with _db() as conn:
        listed = patients.roster(conn)
    # what the page needs to mirror the server's checks: the names that have a
    # label, each drug's first word, and the bounds a reading must sit inside
    return {"patients": listed, "decisions": _decision_counts(),
            "suggest": review.suggestions(corpus), "labeled": review.labeled(corpus),
            "words": review.name_words(corpus),
            "bounds": {k: {"unit": unit, "low": low, "high": high}
                       for k, (unit, low, high) in patients.BOUNDS.items()}}


@staff.post("", status_code=201)
def patient_create(body: dict = Body(...), who: dict = Depends(pharmacist),
                   corpus: dict = Depends(get_corpus)):
    # A patient entered by hand. The same validation as an upload row, with an
    # error per field for the page to put beside it. `entry` is what the page
    # measured about the form itself and holds nothing about the patient.
    row, errors = patients.clean(body)
    entry = body.get("entry")
    if not isinstance(entry, dict):
        entry = {}
    if entry.get("attempt") == 1:
        M["entry"]["first_submit_invalid" if errors else "first_submit_valid"] += 1
    if errors:
        raise HTTPException(422, {"errors": errors})
    with _db() as conn:
        same = patients.find(conn, row["name"], row["dob"])
        if same and body.get("confirm") is not True:
            # checked here and not only on the page: the same name and date of
            # birth, whatever the case, spacing or accents, needs a pharmacist
            # to say it is a second person
            raise HTTPException(409, {"duplicate": len(same),
                                      "message": "a patient with this name and date of birth is "
                                                 "already on file -- confirm to add a second "
                                                 "record"})
        pid = patients.add_patient(conn, row, who["sub"])
        seconds = entry.get("seconds")
        if isinstance(seconds, (int, float)) and 0 < seconds < 86400:
            M["entry_s"].append(round(float(seconds), 1))
        _wake()
        return _profile(conn, pid, corpus)


@staff.get("/questions")
def patient_questions():
    # what patients passed on because the gate would not answer it
    with _db() as conn:
        return {"questions": patients.questions(conn)}


@staff.post("/upload")
async def patient_upload(request: Request, who: dict = Depends(pharmacist)):
    # one row per patient. Valid rows are stored and every rejected row comes
    # back with its line number and reason; the pair checks the new drugs need
    # are queued, so this returns as soon as the rows are in.
    body = await _capped(request)
    if body is None:
        raise HTTPException(413, f"file is larger than {MAX_UPLOAD // 2**20} MB")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, "the file is not UTF-8 text") from exc
    rows, rejected = patients.read_rows(text)
    return await run_in_threadpool(_stored, rows, rejected, who["sub"])


@staff.post("/mock")
def patient_mock(who: dict = Depends(pharmacist), corpus: dict = Depends(get_corpus)):
    # the 25 generated patients, through the same reader and loader as an upload
    rows, rejected = patients.read_rows(patients.mock_csv(review.labeled(corpus)))
    return _stored(rows, rejected, who["sub"])


@staff.get("/{pid}")
def patient_profile(pid: str, corpus: dict = Depends(get_corpus)):
    with _db() as conn:
        return _profile(conn, pid, corpus)


@staff.post("/{pid}/readings")
def patient_readings(pid: str, body: dict = Body(...), who: dict = Depends(pharmacist),
                     corpus: dict = Depends(get_corpus)):
    # an edit is a new reading: the history keeps every one, with who entered it
    with _db() as conn:
        found = _profile(conn, pid, corpus)
        readings, taken, errors = patients.read_indicators(body, born=found["dob"])
        if not errors and not readings:
            errors = {"readings": "nothing to add"}
        if errors:
            raise HTTPException(422, {"errors": errors})
        patients.add_readings(conn, pid, readings, taken, who["sub"])
        conn.commit()
        return _profile(conn, pid, corpus)


@staff.post("/{pid}/drugs")
def patient_add_drug(pid: str, body: dict = Body(...), who: dict = Depends(pharmacist),
                     corpus: dict = Depends(get_corpus)):
    name = " ".join(str(body.get("name") or "").split())
    with _db() as conn:
        _profile(conn, pid, corpus)
        if not 0 < len(name) <= 80:
            raise HTTPException(422, {"errors": {"drug": "a drug name, up to 80 characters"}})
        patients.add_drugs(conn, pid, [name], who["sub"])
        conn.commit()
        _wake()
        return _profile(conn, pid, corpus)


class Decision(BaseModel):
    item: str = Field(..., min_length=16, max_length=16)
    verdict: Literal["accepted", "overridden"]
    reason: str = Field("", max_length=500)


def _report(conn, pid: str, labels: review.Labels) -> dict:
    cfg = load_config()
    found = review.report(conn, pid, labels, cfg.llm, cfg.db)
    if found is None:
        raise HTTPException(404, "no such patient")
    return found


@staff.get("/{pid}/report")
def patient_report(pid: str, labels: review.Labels = Depends(get_labels)):
    # from the cache: it says how many pairs are checked and names the rest
    with _db() as conn:
        return _report(conn, pid, labels)


@staff.post("/{pid}/decisions")
def patient_decide(pid: str, req: Decision, who: dict = Depends(pharmacist),
                   labels: review.Labels = Depends(get_labels)):
    # the pharmacist decides: each item is accepted or overridden, and the
    # decision is a new row -- changing one's mind adds another
    with _db() as conn:
        if req.item not in {i.get("key") for i in _report(conn, pid, labels)["items"]}:
            raise HTTPException(404, "that item is not on this patient's report")
        patients.decide(conn, pid, req.item, req.verdict, req.reason.strip(), who["sub"])
        return _report(conn, pid, labels)


class LoginName(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)


@staff.post("/{pid}/login")
def patient_login(pid: str, req: LoginName, corpus: dict = Depends(get_corpus)):
    # A pharmacist creates each patient's login, for a record that exists. The
    # password in the answer works once: it is shown to the pharmacist to hand
    # over, never logged, and the first sign-in has to replace it.
    with _db() as conn:
        _profile(conn, pid, corpus)
        try:
            username, once = patients.set_login(conn, pid, req.username, load_config().scrypt_n)
        except ValueError as exc:
            raise HTTPException(422, {"errors": {"username": str(exc)}}) from exc
    return {"username": username, "one_time_password": once}


@staff.delete("/{pid}/drugs/{drug}")
def patient_stop_drug(pid: str, drug: int, corpus: dict = Depends(get_corpus)):
    with _db() as conn:
        if not patients.stop_drug(conn, pid, drug):
            raise HTTPException(404, "no such drug on this patient")
        return _profile(conn, pid, corpus)


# ── patient routes: the patient's own ────────────────────────────────────────
# No id in these paths: the record is the one named by the caller's session, so
# there is no URL that asks for somebody else's.

def _mine(me: dict, corpus: dict) -> tuple[str, list[dict], list[review.Drug]]:
    # the caller's own name and drugs, and the labeled ones among them, each once
    with _db() as conn:
        found = patients.profile(conn, me["pid"])
    if found is None:
        raise HTTPException(404, "no such patient")
    tagged = review.tag(found["drugs"], corpus)
    known = review.drugs(corpus)
    return found["name"], tagged, [known[k] for k in
                                   dict.fromkeys(d["drug"] for d in tagged if d["drug"])]


@own.get("")
def my_record(me: dict = Depends(patient), corpus: dict = Depends(get_corpus)):
    # the medication list -- and nothing a pharmacist sees beyond it: no
    # indicators, no interaction categories, no recommendations
    name, tagged, _ = _mine(me, corpus)
    return {"name": name, "drugs": [{"name": d["name"], "label": d["label"]} for d in tagged]}


@own.get("/counseling")
def my_counseling(me: dict = Depends(patient), labels: review.Labels = Depends(get_labels)):
    # section 17 of each drug's label, word for word with its pages, from the
    # same function `pharmacli docs handout` prints
    from pharmacli import safety

    _, tagged, mine = _mine(me, labels.corpus)
    out: list[dict] = []
    for drug in mine:
        try:
            out.append({"drug": drug.name,
                        "text": safety.handout(drug.key, labels.corpus, labels.docs_dir)})
        except (LookupError, OSError) as exc:      # an older label with no section 17, or no PDF
            out.append({"drug": drug.name, "text": None,
                        "note": f"{exc}. Ask your pharmacist about this medicine."})
    out += [{"drug": d["name"], "text": None,
             "note": "There is no label on file for this medicine. Ask your pharmacist about it."}
            for d in tagged if not d["drug"]]
    return {"counseling": out}


@own.get("/recalls")
def my_recalls(me: dict = Depends(patient), corpus: dict = Depends(get_corpus)):
    # FDA recalls that name a package of one of the patient's drugs, each with its number
    _, _, mine = _mine(me, corpus)
    items, problem = review.recall_items(mine)
    return {"recalls": [{"title": i["title"], "note": i["note"], **i["quotes"][0]} for i in items],
            "note": problem}


class Question(BaseModel):
    question: str = Field(..., min_length=3, max_length=500)


@own.post("/ask")
async def my_question(req: Question, me: dict = Depends(patient),
                      r: rag.Rag = Depends(get_rag)):
    # label quotes or a refusal; the gate decides before anything is retrieved
    try:
        return await review.patient_answer(r, req.question)
    except httpx.HTTPError as exc:
        raise HTTPException(503, "label questions are unavailable right now: the counseling "
                                 "text on this page still is") from exc


@own.post("/forward")
def my_forward(req: Question, me: dict = Depends(patient)):
    # the patient chose to pass a question to the pharmacists (the guard audits it)
    with _db() as conn:
        patients.forward(conn, me["pid"], " ".join(req.question.split()))
    return {"ok": True}


# last, after every route above is on its router: include_router copies the
# routes a router holds at the moment it is called. `own` first, so /patients/me
# is never read as a patient id.
app.include_router(own)
app.include_router(staff)
