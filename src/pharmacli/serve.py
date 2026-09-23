# Week 6: the prediction API. Week 10: the same app, in production dress --
# JWT on the data endpoints, the week 9 RAG assistant behind /ask (streaming
# over SSE), one JSON log line per request with a request id, and /metrics.
#
# One app, not two: /predict and /ask share the config, the logging and the
# auth, and a second FastAPI process would have duplicated all three.

import base64
import binascii
import collections
import contextvars
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from pharmacli import rag
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


def issue(sub: str, ttl: int = TTL) -> str:
    now = int(time.time())
    body = (f"{_b64(json.dumps(ALG).encode())}."
            f"{_b64(json.dumps({'sub': sub, 'iat': now, 'exp': now + ttl}).encode())}")
    sig = hmac.new(_secret().encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}"


def verify(token: str) -> dict:
    try:
        head, payload, sig = token.split(".")
        if json.loads(_unb64(head)).get("alg") != ALG["alg"]:
            raise ValueError("unsupported algorithm")
        want = hmac.new(_secret().encode(), f"{head}.{payload}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(want, _unb64(sig)):
            raise ValueError("bad signature")
        claims = json.loads(_unb64(payload))
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise HTTPException(401, f"invalid token: {exc}") from exc
    if float(claims.get("exp", 0)) < time.time():
        raise HTTPException(401, "token expired")
    return claims


bearer = HTTPBearer(auto_error=False, description="a token from `pharmacli token`")


def caller(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    # auto_error=False only to answer a missing header with 401 rather than
    # HTTPBearer's 403: nothing was forbidden, nothing was presented
    if cred is None:
        raise HTTPException(401, "this endpoint needs a bearer token -- run `pharmacli token`")
    return str(verify(cred.credentials).get("sub", "?"))


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
        try:
            S["rag"] = await stack.enter_async_context(rag.session(cfg))
            log.info("documents from %s, model %s", cfg.chroma_url, cfg.llm)
        except (OSError, ValueError) as exc:  # no corpus.json, or an unreadable one
            S["rag_error"] = f"{type(exc).__name__}: {exc}"
            log.warning("/ask is off: %s", S["rag_error"])
        yield
    S.clear()


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
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    REQ_ID.set(rid)
    t = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        M["errors"]["unhandled"] += 1
        log.exception("unhandled error", extra={"fields": {"path": request.url.path}})
        raise
    ms = round((time.perf_counter() - t) * 1000, 1)
    # the path template ("/ask"), not the URL, so metrics do not grow a key per
    # distinct request. For /ask/stream this is time to the first byte: the
    # body is still generating when the middleware returns.
    route = getattr(request.scope.get("route"), "path", request.url.path)
    key = f"{request.method} {route}"
    M["requests"][key] += 1
    M["latency"][key].append(ms)
    if response.status_code >= 400:
        M["errors"][f"{key} {response.status_code}"] += 1
    response.headers["x-request-id"] = rid
    response.headers["x-response-time-ms"] = str(ms)
    log.info("request", extra={"fields": {"method": request.method, "path": request.url.path,
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
                         for k, v in M["stages"].items()}}


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
    out = await rag.collect(r, req.question, k=req.k, chunking=req.chunking)
    _record(out["timings_ms"], out["tokens"], who, out["reason"] or out["error"])
    return out


@app.post("/ask/stream")
async def ask_stream(req: AskIn, who: str = Depends(caller), r: rag.Rag = Depends(get_rag)):
    async def events():
        refused = None
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

# ponytail: one upload slot, because `chat` is one local person looking at their
# own shelf. Add a session id the day it serves more than one.
UPLOAD: dict = {}
MAX_UPLOAD = 5 * 1024 * 1024          # a 5,000-line inventory CSV is ~200KB


@app.get("/chat", response_class=HTMLResponse)
def chat_page():
    # Open, because a browser navigating to a page cannot send a header. The
    # page is a shell: every endpoint below it needs the token, which reaches
    # it in the URL fragment -- the one part of a URL browsers never send.
    return CHAT_PAGE


@app.get("/chat/prompts")
def chat_prompts(who: str = Depends(caller)):
    from pharmacli import ask

    return [label for label, _, _ in ask.QUESTIONS]


@app.get("/chat/ask")
def chat_ask(q: str, who: str = Depends(caller)):
    from pharmacli import ask

    # match() once, here, only to strip the label's own words out of the phrase
    # arg; answer() hands the label back so it is not re-derived
    ranked = ask.match(q)
    guess = ranked[0][1] if ranked and ranked[0][0] >= ask.CUTOFF else ""
    held = UPLOAD.get("held")
    label, lines = ask.answer(q, db=load_config().db, shelf=held and held["shelf"],
                              **ask.args_from(q, guess))
    text = "\n".join(lines)
    if held and label:
        text = f"from {held['name']} (not saved)\n\n{text}"
    return {"label": label, "text": text}


@app.post("/chat/upload")
async def chat_upload(request: Request, who: str = Depends(caller)):
    import tempfile

    from pharmacli import ask
    from pharmacli.cli import read_inventory

    name = request.headers.get("x-filename") or "inventory.csv"
    empty = {"name": name, "lines": 0, "skipped": 0, "reasons": [], "suggest": []}
    # the length header first, so an oversized file is refused before it is read
    if int(request.headers.get("content-length") or 0) > MAX_UPLOAD:
        return empty | {"error": f"file is larger than {MAX_UPLOAD // 2**20} MB"}

    with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as fh:
        fh.write(await request.body())
        path = fh.name
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
    conn = connect(load_config().db)
    try:
        changed = watch_cost(conn, held["rows"])
    finally:
        conn.close()
    UPLOAD.pop("held")          # the shelf holds it now; answer from there
    return {"saved": len(held["rows"]), "changed": changed}


@app.post("/chat/clear")
def chat_clear(who: str = Depends(caller)):
    UPLOAD.pop("held", None)
    return {"ok": True}
