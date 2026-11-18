# Who may reach what. Sign-in, sign-out and expiry; the role guard on every
# route under the patient prefix, generated from the app's own route table so a
# route added later cannot skip it; the audit row each request leaves; and a
# canary patient whose name and birthday must never reach a log line or /metrics.
import io
import json
import logging
import re
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import PASSWORD, patient_login, signin

pytest.importorskip("fastapi")

from pharmacli import patients, serve  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "src" / "pharmacli"
CANARY = {"name": "Zebulon Quixote-Canary", "dob": "1931-07-13",
          "drugs": "lisinopril; unobtainium", "egfr": "41", "allergies": "canary-feather dander"}
QUESTION = {"question": "what does the metformin label say about renal impairment"}


def add(client, raw=CANARY) -> str:
    row, errors = patients.clean(raw)
    assert not errors, errors
    with closing(patients.connect(client.db)) as conn:
        return patients.add_patient(conn, row, "rph")


def audit_rows(client) -> list[dict]:
    with closing(patients.connect(client.db)) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM audit ORDER BY id")]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── the route table ──────────────────────────────────────────────────────────

def routes(table=None, prefix=""):
    # Every route the app answers. FastAPI used to copy an included router's
    # routes into app.routes; newer versions keep the router whole and resolve
    # it per request, so this walks into it. Both shapes end up here.
    for r in serve.app.routes if table is None else table:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from routes(inner.routes, prefix + r.include_context.prefix)
        elif hasattr(r, "dependant"):
            yield SimpleNamespace(path=prefix + r.path, methods=r.methods, dependant=r.dependant)


def patient_routes() -> list:
    return [r for r in routes()
            if r.path == serve.PREFIX or r.path.startswith(serve.PREFIX + "/")]


def is_own(route) -> bool:
    return (route.path + "/").startswith(serve.PREFIX + "/me/")


def guards(route) -> set:
    # every dependency the route runs, including the ones its router added
    found, todo = set(), list(route.dependant.dependencies)
    while todo:
        dep = todo.pop()
        found.add(dep.call)
        todo += dep.dependencies
    return found


def calls(route, pid: str):
    target = re.sub(r"\{(\w+)\}", lambda m: pid if m.group(1) == "pid" else "1", route.path)
    return [(method, target) for method in sorted(route.methods - {"HEAD", "OPTIONS"})]


def teverypatientroutecarriestheguardforitsrole():
    found = patient_routes()
    for route in found:
        want = serve.patient if is_own(route) else serve.pharmacist
        assert want in guards(route), f"{route.path} is not behind the {want.__name__} guard"
    # both kinds exist, so the loop above cannot pass by finding nothing
    assert any(is_own(r) for r in found) and not all(is_own(r) for r in found)
    # and the walk saw everything the app publishes under the prefix
    published = {p for p in serve.app.openapi()["paths"]
                 if p == serve.PREFIX or p.startswith(serve.PREFIX + "/")}
    assert published == {r.path for r in found}


def tapatientrouterefuseseverytokenthatisnotitsrole(clinic):
    pid = add(clinic)
    staff, own = signin(clinic), patient_login(clinic, pid)
    signed_out = signin(clinic)
    assert clinic.post("/auth/logout", headers=signed_out).status_code == 200
    expired = bearer(serve.issue("rph", ttl=-1, role="pharmacist", jti="x"))
    no_role = bearer(serve.issue("cli"))                    # what `pharmacli token` mints
    no_session = bearer(serve.issue("rph", role="pharmacist"))   # a role the server never issued
    for route in patient_routes():
        for method, target in calls(route, pid):
            def status(headers, method=method, target=target):
                return clinic.request(method, target, headers=headers).status_code

            what = f"{method} {route.path}"
            assert status({}) == 401, what
            assert status(bearer("not.a.token")) == 401, what
            assert status(expired) == 401, what
            assert status(signed_out) == 401, what
            assert status(no_session) == 401, what
            assert status(no_role) == 403, what
            assert status(staff if is_own(route) else own) == 403, what      # the wrong role


# ── sign-in, sign-out, expiry, lockout ───────────────────────────────────────

def tsigninissuesaroleandasessionforoneshift(clinic):
    body = clinic.post("/auth/login", json={"username": "rph", "password": PASSWORD}).json()
    claims = serve.verify(body["token"])
    assert body["role"] == claims["role"] == "pharmacist" and body["must_change"] is False
    assert claims["sub"] == "rph" and len(claims["jti"]) == 32 and "pid" not in claims
    assert claims["exp"] - claims["iat"] == 8 * 3600


def tanunknownnameanswersexactlylikeawrongpassword(clinic):
    wrong = clinic.post("/auth/login", json={"username": "rph", "password": "not the password"})
    unknown = clinic.post("/auth/login", json={"username": "nobody", "password": "not the one"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"] == "wrong username or password"


def tthereisnodefaultaccount(clinic):
    with closing(patients.connect(clinic.db)) as conn:
        assert [r["username"] for r in conn.execute("SELECT username FROM account")] == ["rph"]
    for name, password in (("admin", "admin"), ("pharmacist", "pharmacist1234"), ("rph", "rph")):
        r = clinic.post("/auth/login", json={"username": name, "password": password})
        assert r.status_code == 401


def tsignoutendsthesessioneverywhere(clinic):
    token = signin(clinic)
    assert clinic.get("/patients", headers=token).status_code == 200
    assert clinic.get("/chat/prompts", headers=token).status_code == 200    # the older routes too
    assert clinic.post("/auth/logout", headers=token).status_code == 200
    assert clinic.get("/patients", headers=token).status_code == 401
    assert clinic.get("/chat/prompts", headers=token).status_code == 401
    assert clinic.post("/auth/logout", headers=token).status_code == 401
    # a token that never came from signing in has no session to end
    assert clinic.post("/auth/logout", headers=bearer(serve.issue("cli"))).status_code == 403


def tatokenendswhenthesessionlengthdoes(clinic, monkeypatch):
    monkeypatch.setenv("PHARMACLI_SESSION_HOURS", "0")
    r = clinic.get("/patients", headers=signin(clinic))
    assert r.status_code == 401


def trepeatedwrongpasswordslockthename(clinic, monkeypatch):
    monkeypatch.setenv("PHARMACLI_LOCK_AFTER", "2")
    for _ in range(2):
        r = clinic.post("/auth/login", json={"username": "rph", "password": "not the password"})
        assert r.status_code == 401
    locked = clinic.post("/auth/login", json={"username": "rph", "password": PASSWORD})
    assert locked.status_code == 429 and "try again in 15 minutes" in locked.json()["detail"]


def tatokenfromthecliandapharmacistsbothworkwheretheydidbefore(clinic):
    assert clinic.get("/chat/prompts", headers=bearer(serve.issue("cli"))).status_code == 200
    assert clinic.get("/chat/prompts", headers=signin(clinic)).status_code == 200


def tapatienttokenisrefusedoneveryroutethatpredatespatientaccounts(clinic):
    token = patient_login(clinic, add(clinic))
    older = [("POST", "/predict", {"ndc": "00000000001"}), ("POST", "/ask", QUESTION),
             ("POST", "/ask/stream", QUESTION), ("GET", "/chat/prompts", None),
             ("GET", "/chat/ask", None), ("POST", "/chat/upload", None),
             ("POST", "/chat/save", None), ("POST", "/chat/clear", None)]
    for method, path, body in older:
        r = clinic.request(method, path, json=body, params={"q": "what is on my shelf"},
                           headers=token)
        assert r.status_code == 403, f"{method} {path} answered {r.status_code}"
    # and that list is every route behind the older guard: a new one is added on purpose
    guarded = {(m, r.path) for r in routes() if serve.caller in guards(r) for m in r.methods}
    assert guarded == {(m, p) for m, p, _ in older}


def taonetimepasswordmustbereplacedbeforeanythingelse(clinic):
    pid = add(clinic)
    with closing(patients.connect(clinic.db)) as conn:
        _, once = patients.set_login(conn, pid, "pat", 16)
    body = clinic.post("/auth/login", json={"username": "pat", "password": once}).json()
    assert body["role"] == "patient" and body["must_change"] is True
    assert serve.verify(body["token"])["pid"] == pid             # the id of their own record
    token = bearer(body["token"])
    refused = clinic.get("/patients/me", headers=token)
    assert refused.status_code == 403 and "password" in refused.json()["detail"]
    short = clinic.post("/auth/password", json={"current": once, "new": "short"}, headers=token)
    assert short.status_code == 422 and "at least 12" in short.json()["detail"]
    assert clinic.post("/auth/password", json={"current": once, "new": PASSWORD},
                       headers=token).status_code == 200
    assert clinic.get("/patients/me", headers=token).json()["name"] == CANARY["name"]


# ── the audit trail ──────────────────────────────────────────────────────────

def teveryrequestunderthepatientprefixleavesanauditrow(clinic):
    pid = add(clinic)
    allowed = clinic.get("/patients", headers=signin(clinic))
    denied = clinic.get("/patients")
    own = patient_login(clinic, pid)
    clinic.get("/patients", headers=own)                         # the wrong role
    clinic.get("/patients/me", headers=own)
    rows = audit_rows(clinic)
    listed = [r for r in rows if r["action"] == "GET /patients"]
    assert [(r["who"], r["role"], r["allowed"]) for r in listed] == [
        ("rph", "pharmacist", 1), ("-", "-", 0), ("pat", "patient", 0)]
    # the same id the response and the request log carry
    assert listed[0]["request_id"] == allowed.headers["x-request-id"]
    assert listed[1]["request_id"] == denied.headers["x-request-id"]
    assert all(r["at"] for r in rows)
    mine = next(r for r in rows if r["action"] == "GET /patients/me")
    assert (mine["who"], mine["role"], mine["patient"], mine["allowed"]) == (
        "pat", "patient", pid, 1)
    assert [r["allowed"] for r in rows if r["action"] == "sign-in"] == [1, 1, 1]


# ── nothing about a patient in the logs or the metrics ──────────────────────

def tacanarypatientneverreachesthelogsorthemetrics(clinic):
    # the same formatter production uses, on the root logger, at DEBUG
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(serve.JsonLines())
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        pid = add(clinic)
        staff, own = signin(clinic), patient_login(clinic, pid)
        for route in patient_routes():
            for method, target in calls(route, pid):
                clinic.request(method, target, headers=own if is_own(route) else staff)
                clinic.request(method, target)                   # and once refused
        # a mistyped URL under the prefix still carries the id
        assert clinic.get(f"/patients/{pid}/no-such-page", headers=staff).status_code == 404
        metrics = json.dumps(clinic.get("/metrics").json())
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
    logs = stream.getvalue()
    assert '"msg": "request"' in logs and "/patients/(no route)" in logs    # the capture is real
    assert "GET /patients" in metrics and "(no route)" in metrics
    for secret in ("Zebulon", "Quixote", "Canary", "1931-07-13", "canary-feather",
                   "unobtainium", pid):
        assert secret not in logs, f"{secret!r} reached the logs"
        assert secret not in metrics, f"{secret!r} reached /metrics"


# ── the pages and the command line ───────────────────────────────────────────

def tthesignedinpagesareshellsunderastrictcontentpolicy(clinic):
    for page in ("login", "pharmacist", "patient"):
        r = clinic.get(f"/{page}")
        assert r.status_code == 200
        assert r.headers["content-security-policy"].startswith("default-src 'self'")
        # what that policy would block: inline style, inline script, inline handlers
        assert "<style" not in r.text and " style=" not in r.text
        assert not re.search(r"<script(?![^>]*\bsrc=)", r.text)
        assert not re.search(r"\son\w+=", r.text)
    # Their scripts put every dynamic value in as text, never as HTML, and build
    # lists through fill(): a bare replaceChildren() prints "null" for an absent
    # child and "[object HTMLElement]" for an array of them.
    scripts = {name: (SRC / name).read_text(encoding="utf-8")
               for name in ("app.js", "pharmacist.js", "patient.js")}
    for name, text in scripts.items():
        assert "innerHTML" not in text and "insertAdjacentHTML" not in text, name
        assert "document.write" not in text and "eval(" not in text, name
    assert ".replaceChildren(" not in scripts["pharmacist.js"] + scripts["patient.js"]
    # the token is kept for the tab only and sent in a header, never put in a URL
    assert "sessionStorage" in scripts["app.js"] and "localStorage" not in "".join(scripts.values())
    assert "Authorization: 'Bearer '" in scripts["app.js"]
    assert not re.search(r"[?&#]token=", "".join(scripts.values()))
    # the chat keeps its structure, inline script and all, so it has no such policy
    assert "content-security-policy" not in clinic.get("/chat").headers


def ttheclicreatesapharmacistandneverprintsorstoresthepassword(tmp_path, monkeypatch, capsys):
    import getpass

    from pharmacli.cli import EXIT_BADIN, EXIT_OK, main

    db = tmp_path / "cli.db"
    monkeypatch.setenv("PHARMACLI_PATIENT_DB", str(db))
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": PASSWORD)
    assert main(["patients", "account", "alice"]) == EXIT_OK
    assert main(["patients", "account", "alice"]) == EXIT_BADIN          # the name is taken
    answers = iter(["one long password", "another long one"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    assert main(["patients", "account", "bob"]) == EXIT_BADIN            # the two differ
    assert main(["patients", "account"]) == EXIT_BADIN                   # no name
    out = capsys.readouterr()
    assert PASSWORD not in out.out + out.err
    with closing(patients.connect(str(db))) as conn:
        assert [tuple(r) for r in conn.execute("SELECT username, role FROM account")] == [
            ("alice", "pharmacist")]
    assert PASSWORD.encode() not in db.read_bytes()
