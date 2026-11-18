# Patient profiles, the accounts that may open them and the audit trail, in a
# SQLite file of their own. Standard library only, so `pharmacli patients ...`
# runs on the core install.
#
# SYNTHETIC PATIENTS ONLY. Storage, access control and audit logging here are
# not yet ready for protected health information under HIPAA.
#
# Why a second database: pharmacli.db is snapshotted with DVC and handed around
# as "the price panel", and a patient row must never travel with it. This file
# is git-ignored, outside DVC, outside the image, and on its own Compose volume.

import csv
import hashlib
import hmac
import io
import random
import re
import secrets
import sqlite3
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone

ROLES = ("pharmacist", "patient")
MIN_PASSWORD = 12                # length only: no composition rules
APPEND_ONLY = ("audit", "reading", "decision")

SCHEMA = """
CREATE TABLE IF NOT EXISTS patient (
    id TEXT PRIMARY KEY,                 -- random and opaque: the only thing a URL carries
    name TEXT NOT NULL, dob TEXT NOT NULL,
    match TEXT NOT NULL,                 -- name and dob, normalized, to catch duplicates
    created TEXT NOT NULL, created_by TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS patient_match ON patient(match);
CREATE TABLE IF NOT EXISTS drug (
    id INTEGER PRIMARY KEY, patient TEXT NOT NULL REFERENCES patient(id),
    name TEXT NOT NULL,                  -- as entered; the label match is worked out on read
    added TEXT NOT NULL, added_by TEXT NOT NULL, stopped TEXT);
CREATE TABLE IF NOT EXISTS reading (     -- indicator history: the current value is the newest row
    id INTEGER PRIMARY KEY, patient TEXT NOT NULL REFERENCES patient(id),
    indicator TEXT NOT NULL, value TEXT NOT NULL, taken TEXT NOT NULL,
    entered TEXT NOT NULL, entered_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('pharmacist', 'patient')),
    password TEXT NOT NULL,              -- scrypt$N$r$p$salt$hash: the parameters beside the hash
    patient TEXT REFERENCES patient(id), -- a patient account's own record
    must_change INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session (     -- what makes sign-out and expiry enforceable
    jti TEXT PRIMARY KEY, account INTEGER NOT NULL REFERENCES account(id),
    expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS attempt (     -- wrong passwords, by the name typed, known or not
    username TEXT PRIMARY KEY, fails INTEGER NOT NULL, at REAL NOT NULL,
    until REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, who TEXT NOT NULL, role TEXT NOT NULL,
    action TEXT NOT NULL, patient TEXT, request_id TEXT NOT NULL, allowed INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pair (        -- what two labels say about each other: no patient in it
    a TEXT NOT NULL, b TEXT NOT NULL, labels TEXT NOT NULL, prompt TEXT NOT NULL,
    model TEXT NOT NULL, result TEXT NOT NULL, checked TEXT NOT NULL,
    PRIMARY KEY (a, b, labels, prompt, model));
CREATE TABLE IF NOT EXISTS decision (
    id INTEGER PRIMARY KEY, patient TEXT NOT NULL REFERENCES patient(id), item TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('accepted', 'overridden')),
    reason TEXT NOT NULL DEFAULT '', by TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS question (    -- a patient's question, passed to the pharmacists
    id INTEGER PRIMARY KEY, patient TEXT NOT NULL REFERENCES patient(id),
    text TEXT NOT NULL, at TEXT NOT NULL);
""" + "".join(
    # append-only in the database itself, not by convention: a correction is a new row
    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{verb.lower()} BEFORE {verb} ON {table} "
    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;\n"
    for table in APPEND_ONLY for verb in ("UPDATE", "DELETE"))


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── passwords: scrypt from the standard library ──────────────────────────────

def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    # the work area is 128*r*N bytes (134 MB at N=2^17, r=8); OpenSSL's 32 MiB
    # default rejects anything above N=2^14 at r=8, so the limit is raised to fit
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p,
                          maxmem=2 * 128 * n * r, dklen=32)


def hash_password(password: str, n: int) -> str:
    salt = secrets.token_bytes(16)
    return f"scrypt${n}$8$1${salt.hex()}${_scrypt(password, salt, n, 8, 1).hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, want = stored.split("$")
        got = _scrypt(password, bytes.fromhex(salt), int(n), int(r), int(p))
    except ValueError:                    # not one of ours, or parameters OpenSSL refuses
        return False
    return kind == "scrypt" and hmac.compare_digest(got, bytes.fromhex(want))


def _username(raw: str) -> str:
    name = raw.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,31}", name):
        raise ValueError("a username is 3 to 32 letters, digits, dots, dashes or underscores")
    return name


def _password(raw: str) -> str:
    if len(raw) < MIN_PASSWORD:
        raise ValueError(f"a password needs at least {MIN_PASSWORD} characters")
    return raw


def add_account(conn: sqlite3.Connection, username: str, password: str, role: str, n: int,
                patient: str | None = None, must_change: bool = False) -> str:
    name = _username(username)
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    try:
        conn.execute("INSERT INTO account (username, role, password, patient, must_change,"
                     " created) VALUES (?,?,?,?,?,?)",
                     (name, role, hash_password(_password(password), n), patient,
                      int(must_change), _now()))
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"the username {name} is taken") from exc
    conn.commit()
    return name


class Locked(Exception):
    def __init__(self, seconds: float):
        super().__init__(f"locked for {seconds:.0f}s")
        self.seconds = seconds


_DUMMY: dict[int, str] = {}


def sign_in(conn: sqlite3.Connection, username: str, password: str, n: int,
            lock_after: int, lock_seconds: float, now: float | None = None):
    # -> the account row, or None for a wrong name or password. The two are not
    # told apart: an unknown name is hashed against a dummy at the same cost and
    # counted towards the same lock, so neither the answer nor the time it took
    # says which names exist.
    now = time.time() if now is None else now
    name = username.strip().lower()[:64]
    conn.execute("DELETE FROM attempt WHERE at < ? AND until < ?", (now - lock_seconds, now))
    tried = conn.execute("SELECT fails, until FROM attempt WHERE username = ?", (name,)).fetchone()
    if tried and tried["until"] > now:
        conn.commit()
        raise Locked(tried["until"] - now)
    acct = conn.execute("SELECT * FROM account WHERE username = ?", (name,)).fetchone()
    if n not in _DUMMY:
        _DUMMY[n] = hash_password(secrets.token_hex(16), n)
    ok = verify_password(password, acct["password"] if acct else _DUMMY[n])
    if not (acct and ok):
        fails = (tried["fails"] if tried else 0) + 1
        conn.execute("INSERT OR REPLACE INTO attempt (username, fails, at, until) VALUES (?,?,?,?)",
                     (name, fails, now, now + lock_seconds if fails >= lock_after else 0))
        conn.commit()
        return None
    conn.execute("DELETE FROM attempt WHERE username = ?", (name,))
    conn.commit()
    return acct


def open_session(conn: sqlite3.Connection, account: int, ttl: float,
                 now: float | None = None) -> str:
    jti = secrets.token_hex(16)
    conn.execute("INSERT INTO session (jti, account, expires) VALUES (?,?,?)",
                 (jti, account, (time.time() if now is None else now) + ttl))
    conn.commit()
    return jti


def session(conn: sqlite3.Connection, jti: str, now: float | None = None):
    # the account behind a live session, or None once it is signed out or expired
    return conn.execute(
        "SELECT a.* FROM session s JOIN account a ON a.id = s.account"
        " WHERE s.jti = ? AND s.revoked = 0 AND s.expires > ?",
        (jti, time.time() if now is None else now)).fetchone()


def close_session(conn: sqlite3.Connection, jti: str) -> None:
    conn.execute("UPDATE session SET revoked = 1 WHERE jti = ?", (jti,))
    conn.commit()


def change_password(conn: sqlite3.Connection, username: str, current: str, new: str,
                    n: int) -> None:
    acct = conn.execute("SELECT * FROM account WHERE username = ?", (username,)).fetchone()
    if not acct or not verify_password(current, acct["password"]):
        raise ValueError("the current password is wrong")
    if new == current:
        raise ValueError("the new password must differ from the current one")
    conn.execute("UPDATE account SET password = ?, must_change = 0 WHERE id = ?",
                 (hash_password(_password(new), n), acct["id"]))
    conn.commit()


def set_login(conn: sqlite3.Connection, pid: str, username: str, n: int) -> tuple[str, str]:
    # -> (username, one-time password). The pharmacist hands the password over
    # once; the first sign-in has to replace it. Asking again for a patient who
    # already has a login resets it and signs them out everywhere.
    once = secrets.token_urlsafe(12)
    have = conn.execute("SELECT id, username FROM account WHERE patient = ?", (pid,)).fetchone()
    if not have:
        return add_account(conn, username, once, "patient", n, patient=pid, must_change=True), once
    conn.execute("UPDATE account SET password = ?, must_change = 1 WHERE id = ?",
                 (hash_password(once, n), have["id"]))
    conn.execute("UPDATE session SET revoked = 1 WHERE account = ?", (have["id"],))
    conn.commit()
    return have["username"], once


# ── the audit trail ──────────────────────────────────────────────────────────

def audit(conn: sqlite3.Connection, who: str, role: str, action: str, allowed: bool,
          request_id: str, patient: str | None = None) -> None:
    conn.execute("INSERT INTO audit (at, who, role, action, patient, request_id, allowed)"
                 " VALUES (?,?,?,?,?,?,?)",
                 (_now(), who, role, action, patient, request_id, int(allowed)))
    conn.commit()


# ── profiles ─────────────────────────────────────────────────────────────────

# indicator -> (unit, lowest, highest). Outside these a value is a typing
# mistake, not a finding: they are sanity checks on data entry and NOT clinical
# thresholds. The clinical triggers live in review.py, each with its source.
BOUNDS = {
    "systolic":  ("mmHg", 50, 300),
    "diastolic": ("mmHg", 20, 200),
    "egfr":      ("mL/min/1.73 m2", 0, 200),
    "crcl":      ("mL/min", 0, 300),
    "alt":       ("U/L", 0, 10000),
    "ast":       ("U/L", 0, 10000),
    "potassium": ("mmol/L", 1, 10),
    "weight_kg": ("kg", 0.5, 500),
}
INDICATORS = (*BOUNDS, "pregnant", "allergies")
# the upload's columns, read by header name; `drugs` holds names separated by ;
COLUMNS = ("name", "dob", "drugs", *INDICATORS, "taken")
REQUIRED = ("name", "dob", "drugs")
_YES_NO = {"yes": "yes", "y": "yes", "true": "yes", "no": "no", "n": "no", "false": "no"}


def match_key(name: str, dob: str) -> str:
    # case, spacing and accents must not hide a duplicate: "José  ALAN" is "jose alan"
    flat = unicodedata.normalize("NFKD", name)
    plain = "".join(c for c in flat if not unicodedata.combining(c))
    return f"{' '.join(plain.casefold().split())}|{dob}"


def _day(raw: str, what: str, today: date, earliest: date) -> tuple[str, str | None]:
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return "", f"{what} must be a date written YYYY-MM-DD"
    if day > today:
        return "", f"{what} {raw} is in the future"
    if day < earliest:
        return "", f"{what} {raw} is before {earliest}"
    return day.isoformat(), None


def _text(raw: dict, key: str) -> str:
    value = raw.get(key)
    return " ".join(str(value).split()) if value is not None else ""


def read_indicators(raw: dict, today: date | None = None,
                    born: str = "") -> tuple[dict[str, str], str, dict[str, str]]:
    # -> (readings to store, the day they were taken, {field: what is wrong})
    today = today or date.today()
    readings: dict[str, str] = {}
    errors: dict[str, str] = {}
    for key, (unit, low, high) in BOUNDS.items():
        cell = _text(raw, key)
        if not cell:
            continue
        try:
            value = float(cell)
        except ValueError:
            errors[key] = f"{cell!r} is not a number"
            continue
        if not low <= value <= high:              # also false for nan
            errors[key] = f"{value:g} is outside {low:g} to {high:g} {unit}"
        else:
            readings[key] = f"{value:g}"
    if ("systolic" in readings) != ("diastolic" in readings) \
            and not {"systolic", "diastolic"} & set(errors):
        errors["diastolic" if "systolic" in readings else "systolic"] = \
            "blood pressure needs both numbers"
    elif "diastolic" in readings and float(readings["diastolic"]) >= float(readings["systolic"]):
        errors["diastolic"] = (f"diastolic {readings['diastolic']} is not below "
                               f"systolic {readings['systolic']}")
    pregnant = _text(raw, "pregnant").lower()
    if pregnant in _YES_NO:
        readings["pregnant"] = _YES_NO[pregnant]
    elif pregnant:
        errors["pregnant"] = "yes or no"
    allergies = _text(raw, "allergies")
    if len(allergies) > 500:
        errors["allergies"] = "up to 500 characters"
    elif allergies:
        readings["allergies"] = allergies

    taken = today.isoformat()
    if _text(raw, "taken"):
        taken, why = _day(_text(raw, "taken"), "the reading date", today,
                          date.fromisoformat(born) if born else date(1900, 1, 1))
        if why:
            errors["taken"] = why
    return readings, taken, errors


def clean(raw: dict, today: date | None = None) -> tuple[dict, dict[str, str]]:
    # -> (the row ready to store, {field: what is wrong with it}). One function
    # for the upload and for manual entry, so the two cannot disagree; the page
    # mirrors these messages but this is the authority.
    today = today or date.today()
    errors: dict[str, str] = {}

    name = _text(raw, "name")
    if not name:
        errors["name"] = "a name is required"
    elif len(name) > 120 or not any(c.isalpha() for c in name):
        errors["name"] = "a name is up to 120 characters and has a letter in it"

    dob, why = _day(_text(raw, "dob"), "the date of birth", today, date(1900, 1, 1))
    if not _text(raw, "dob"):
        errors["dob"] = "a date of birth is required"
    elif why:
        errors["dob"] = why

    listed = raw.get("drugs") or []
    if isinstance(listed, str):
        listed = listed.split(";")
    drugs: list[str] = []
    for item in listed if isinstance(listed, list) else []:
        drug = " ".join(str(item).split())
        if drug and drug.casefold() not in {d.casefold() for d in drugs}:
            drugs.append(drug)
    if not drugs:
        errors["drugs"] = "at least one drug is required"
    elif len(drugs) > 30 or any(len(d) > 80 for d in drugs):
        errors["drugs"] = "up to 30 drugs, each up to 80 characters"

    readings, taken, wrong = read_indicators(raw, today, dob)
    return ({"name": name, "dob": dob, "drugs": drugs, "readings": readings, "taken": taken},
            errors | wrong)


def find(conn: sqlite3.Connection, name: str, dob: str) -> list[str]:
    return [r["id"] for r in conn.execute("SELECT id FROM patient WHERE match = ? ORDER BY created",
                                          (match_key(name, dob),))]


def add_readings(conn: sqlite3.Connection, pid: str, readings: dict[str, str], taken: str,
                 by: str) -> int:
    added = 0
    for indicator, value in readings.items():
        # the same value for the same day is the same reading: uploading a file
        # twice must not double the history
        if conn.execute("SELECT 1 FROM reading WHERE patient = ? AND indicator = ? AND value = ?"
                        " AND taken = ?", (pid, indicator, value, taken)).fetchone():
            continue
        conn.execute("INSERT INTO reading (patient, indicator, value, taken, entered, entered_by)"
                     " VALUES (?,?,?,?,?,?)", (pid, indicator, value, taken, _now(), by))
        added += 1
    return added


def add_drugs(conn: sqlite3.Connection, pid: str, names: list[str], by: str) -> int:
    have = {r["name"].casefold() for r in conn.execute(
        "SELECT name FROM drug WHERE patient = ? AND stopped IS NULL", (pid,))}
    added = 0
    for name in names:
        if name.casefold() not in have:
            conn.execute("INSERT INTO drug (patient, name, added, added_by) VALUES (?,?,?,?)",
                         (pid, name, _now(), by))
            have.add(name.casefold())
            added += 1
    return added


def stop_drug(conn: sqlite3.Connection, pid: str, drug: int) -> bool:
    # stopped, not deleted: the row stays as the record that it was once taken
    done = conn.execute("UPDATE drug SET stopped = ? WHERE id = ? AND patient = ?"
                        " AND stopped IS NULL", (_now(), drug, pid)).rowcount
    conn.commit()
    return bool(done)


def add_patient(conn: sqlite3.Connection, row: dict, by: str) -> str:
    pid = secrets.token_hex(8)
    conn.execute("INSERT INTO patient (id, name, dob, match, created, created_by)"
                 " VALUES (?,?,?,?,?,?)",
                 (pid, row["name"], row["dob"], match_key(row["name"], row["dob"]), _now(), by))
    add_drugs(conn, pid, row["drugs"], by)
    add_readings(conn, pid, row["readings"], row["taken"], by)
    conn.commit()
    return pid


def roster(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT p.id, p.name, p.dob, COUNT(d.id) AS drugs,"
        " EXISTS (SELECT 1 FROM account a WHERE a.patient = p.id) AS login"
        " FROM patient p LEFT JOIN drug d ON d.patient = p.id AND d.stopped IS NULL"
        " GROUP BY p.id ORDER BY p.name, p.dob")]


def drugs(conn: sqlite3.Connection, pid: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, name, added, added_by FROM drug WHERE patient = ? AND stopped IS NULL"
        " ORDER BY id", (pid,))]


def profile(conn: sqlite3.Connection, pid: str) -> dict | None:
    who = conn.execute("SELECT id, name, dob FROM patient WHERE id = ?", (pid,)).fetchone()
    if who is None:
        return None
    history = [dict(r) for r in conn.execute(
        "SELECT indicator, value, taken, entered, entered_by FROM reading WHERE patient = ?"
        " ORDER BY taken DESC, id DESC", (pid,))]
    current: dict[str, dict] = {}
    for reading in history:                      # newest first, so the first seen is current
        current.setdefault(reading["indicator"], reading)
    login = conn.execute("SELECT username FROM account WHERE patient = ?", (pid,)).fetchone()
    return dict(who) | {"drugs": drugs(conn, pid), "current": current, "history": history,
                        "login": login["username"] if login else None}


def drug_names(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    # (patient, drug as entered) for every drug still being taken, by anyone
    return [(r["patient"], r["name"]) for r in conn.execute(
        "SELECT patient, name FROM drug WHERE stopped IS NULL ORDER BY id")]


# ── what two labels say about each other, kept so it is asked once ──────────
# No patient is in a row: the key is the two drugs, their label versions, and
# the prompt and model that produced the answer. A new label version, prompt
# or model is a different key, so the old answer simply stops being found.

def pair_get(conn: sqlite3.Connection, a: str, b: str, labels: str, prompt: str, model: str):
    import json

    row = conn.execute("SELECT result FROM pair WHERE a = ? AND b = ? AND labels = ?"
                       " AND prompt = ? AND model = ?", (a, b, labels, prompt, model)).fetchone()
    return json.loads(row["result"]) if row else None


def pair_put(conn: sqlite3.Connection, a: str, b: str, labels: str, prompt: str, model: str,
             result: dict) -> None:
    import json

    conn.execute("INSERT OR REPLACE INTO pair (a, b, labels, prompt, model, result, checked)"
                 " VALUES (?,?,?,?,?,?,?)",
                 (a, b, labels, prompt, model, json.dumps(result), _now()))
    conn.commit()


# ── a pharmacist's decisions: append-only, the newest one stands ────────────

def decide(conn: sqlite3.Connection, pid: str, item: str, verdict: str, reason: str,
           by: str) -> None:
    conn.execute("INSERT INTO decision (patient, item, verdict, reason, by, at)"
                 " VALUES (?,?,?,?,?,?)", (pid, item, verdict, reason, by, _now()))
    conn.commit()


_LATEST = ("SELECT patient, item, verdict, reason, by, at FROM decision WHERE id IN"
           " (SELECT MAX(id) FROM decision GROUP BY patient, item)")


def decisions(conn: sqlite3.Connection, pid: str) -> dict[str, dict]:
    return {r["item"]: {k: r[k] for k in ("verdict", "reason", "by", "at")}
            for r in conn.execute(_LATEST + " AND patient = ?", (pid,))}


def decision_counts(conn: sqlite3.Connection) -> dict[str, int]:
    # how often a pharmacist overrode what the labels suggested: counts, so the
    # rate can be read without a patient in it
    got = dict(conn.execute(f"SELECT verdict, COUNT(*) FROM ({_LATEST}) GROUP BY verdict"))
    return {"accepted": got.get("accepted", 0), "overridden": got.get("overridden", 0)}


# ── questions a patient passed to the pharmacists ───────────────────────────

def forward(conn: sqlite3.Connection, pid: str, text: str) -> None:
    conn.execute("INSERT INTO question (patient, text, at) VALUES (?,?,?)", (pid, text, _now()))
    conn.commit()


def questions(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT q.id, q.patient, p.name, q.text, q.at FROM question q"
        " JOIN patient p ON p.id = q.patient ORDER BY q.id DESC")]


# ── upload: one row per patient ──────────────────────────────────────────────

def read_rows(text: str, today: date | None = None) \
        -> tuple[list[tuple[int, dict]], list[tuple[int, str]]]:
    # -> (accepted rows with their line numbers, rejected (line, reason)), the
    # way the inventory and claims readers report a file
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    header = [h.strip().lower() for h in reader.fieldnames or []]
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        return [], [(1, f"the header row needs these columns: {', '.join(missing)}")]
    reader.fieldnames = header
    rows, rejected = [], []
    for raw in reader:
        line = reader.line_num
        if None in raw:                           # more cells than the header has columns
            rejected.append((line, "more cells than the header has columns"))
            continue
        row, errors = clean(raw, today)
        if errors:
            rejected.append((line, "; ".join(f"{k}: {why}" for k, why in errors.items())))
        else:
            rows.append((line, row))
    return rows, rejected


def load(conn: sqlite3.Connection, rows: list[tuple[int, dict]], by: str) -> dict:
    created, updated, rejected = [], [], []
    for line, row in rows:
        same = find(conn, row["name"], row["dob"])
        if len(same) > 1:
            rejected.append((line, f"matches {len(same)} existing patients with this name and "
                                   "date of birth; add the reading in the one you mean"))
        elif same:
            # a second upload adds readings to the patient it names: updated, not created
            add_drugs(conn, same[0], row["drugs"], by)
            add_readings(conn, same[0], row["readings"], row["taken"], by)
            updated.append(same[0])
        else:
            created.append(add_patient(conn, row, by))
    conn.commit()
    return {"created": created, "updated": updated, "rejected": rejected}


def template() -> str:
    example = ("Ada Example", "1961-04-09", "lisinopril; potassium chloride", "128", "82", "54",
               "", "31", "28", "4.6", "71", "no", "penicillin", "")
    return ",".join(COLUMNS) + "\n" + ",".join(example) + "\n"


# ── mock patients ────────────────────────────────────────────────────────────

# the pairs the interaction answer key starts from, by the first word of each
# generic name; a mock patient gets one wherever both drugs have a label
KEY_PAIRS = (("aripiprazole", "fluoxetine"), ("aripiprazole", "carbamazepine"),
             ("lamotrigine", "carbamazepine"), ("lisinopril", "potassium"),
             ("oxycodone", "gabapentin"))
_FIRST = ("Ada", "Ben", "Cleo", "Dev", "Esme", "Finn", "Gia", "Hugo", "Ines", "Jude", "Kai",
          "Lena", "Milo", "Nora", "Otis", "Pia", "Quinn", "Rosa", "Sam", "Tess", "Umar", "Vera",
          "Wes", "Xena", "Yuri")
_LAST = ("Example", "Sample", "Testcase", "Placeholder", "Mockford")
_ALLERGIES = ("", "", "", "penicillin", "sulfonamides", "latex")
# readings are dated back from here, not from today, so the same seed gives the
# same bytes whenever it runs
_ANCHOR = date(2026, 11, 1)


def mock_csv(labeled: dict[str, str], seed: int = 7, count: int = 25) -> str:
    # ponytail: generated patients so profiles can be built and demoed before a
    # real export exists -- and so nothing here is ever a real person. `labeled`
    # maps a drug to the name to write for it (review.labeled), so every drug
    # has a label and every pair can be checked.
    rng = random.Random(seed)
    names = sorted(labeled.values())
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(COLUMNS)
    for i in range(count):
        pair = KEY_PAIRS[i] if i < len(KEY_PAIRS) else ()
        mine = [labeled[k] for k in pair] if all(k in labeled for k in pair) else []
        wanted = rng.randint(max(2, len(mine)), 6)
        spare = [n for n in names if n not in mine]
        mine += rng.sample(spare, min(wanted - len(mine), len(spare)))
        row = {"name": f"{_FIRST[i % len(_FIRST)]} {_LAST[(i + i // len(_FIRST)) % len(_LAST)]}",
               "dob": date(rng.randrange(1940, 2005), rng.randrange(1, 13),
                           rng.randrange(1, 29)).isoformat(),
               "drugs": "; ".join(mine),
               "systolic": rng.randrange(104, 162), "diastolic": rng.randrange(62, 96),
               "egfr": rng.randrange(62, 112), "alt": rng.randrange(12, 38),
               "ast": rng.randrange(12, 36), "potassium": f"{rng.uniform(3.6, 5.0):.1f}",
               "weight_kg": rng.randrange(52, 112), "pregnant": "",
               "allergies": rng.choice(_ALLERGIES),
               "taken": (_ANCHOR - timedelta(days=rng.randrange(120))).isoformat()}
        # the first four cover each path a report can take: low kidney function,
        # raised liver enzymes, pregnancy, and nothing notable
        if i == 0:
            row["egfr"] = 38
        elif i == 1:
            row |= {"alt": 96, "ast": 88}
        elif i == 2:
            row |= {"pregnant": "yes", "dob": date(1994, rng.randrange(1, 13),
                                                   rng.randrange(1, 29)).isoformat()}
        elif i == 3:
            row["allergies"] = ""
        w.writerow([row.get(c, "") for c in COLUMNS])
    return out.getvalue()
