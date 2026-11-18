# The patient database on its own: passwords, sign-in, sessions, the audit
# trail, and profiles. No server and no model -- this layer is standard library
# only, which is what lets `pharmacli patients ...` run on the core install.
import json
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from conftest import PASSWORD, PATIENT_CORPUS

from pharmacli import patients, review

TODAY = date(2026, 11, 16)
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def conn(tmp_path):
    with closing(patients.connect(str(tmp_path / "p.db"))) as c:
        yield c


# ── passwords and sign-in ────────────────────────────────────────────────────

def thashesaresaltedandcarrytheirparameters():
    one, two = (patients.hash_password(PASSWORD, 16) for _ in range(2))
    assert one != two                                   # a salt of its own each time
    kind, n, r, p, salt, digest = one.split("$")
    assert (kind, n, r, p) == ("scrypt", "16", "8", "1")
    assert len(bytes.fromhex(salt)) == 16 and len(bytes.fromhex(digest)) == 32
    assert patients.verify_password(PASSWORD, one) and patients.verify_password(PASSWORD, two)
    assert not patients.verify_password(PASSWORD + "x", one)
    assert not patients.verify_password(PASSWORD, "not$a$hash")


def tthedefaultcostfitsintheraisedmemorylimit():
    # N=2^17 at r=8 needs 134 MB. OpenSSL's own 32 MiB limit refuses it, so this
    # only passes because maxmem is raised to fit -- and it is the cost that ships
    from pharmacli.config import DEFAULTS

    n = int(DEFAULTS["PHARMACLI_SCRYPT_N"])
    assert n == 2 ** 17
    stored = patients.hash_password(PASSWORD, n)
    assert stored.startswith("scrypt$131072$8$1$") and patients.verify_password(PASSWORD, stored)


def taccountsneedtwelvecharactersandafreename(conn):
    assert patients.add_account(conn, " RPH ", PASSWORD, "pharmacist", 16) == "rph"
    with pytest.raises(ValueError, match="at least 12"):
        patients.add_account(conn, "short", "elevenchars", "pharmacist", 16)
    # length is the only rule: no digit or symbol is demanded
    patients.add_account(conn, "plain", "aaaaaaaaaaaa", "pharmacist", 16)
    with pytest.raises(ValueError, match="taken"):
        patients.add_account(conn, "rph", PASSWORD, "pharmacist", 16)
    with pytest.raises(ValueError, match="username"):
        patients.add_account(conn, "a b", PASSWORD, "pharmacist", 16)
    with pytest.raises(ValueError, match="role"):
        patients.add_account(conn, "boss", PASSWORD, "admin", 16)
    stored = conn.execute("SELECT password FROM account WHERE username = 'rph'").fetchone()[0]
    assert PASSWORD not in stored                       # only the salted hash is kept


def twrongpasswordslockthenameknownornot(conn, monkeypatch):
    patients.add_account(conn, "rph", PASSWORD, "pharmacist", 16)
    hashed = []
    real = patients.verify_password
    monkeypatch.setattr(patients, "verify_password",
                        lambda pw, stored: hashed.append(stored) or real(pw, stored))

    def attempt(name, password, at):
        return patients.sign_in(conn, name, password, 16, 3, 60.0, now=at)

    for name in ("rph", "nobody"):
        for i in range(3):
            assert attempt(name, "wrong password!", 1000.0 + i) is None
        # locked: even the right password is refused, and nothing is hashed
        before = len(hashed)
        with pytest.raises(patients.Locked) as locked:
            attempt(name, PASSWORD, 1010.0)
        assert 0 < locked.value.seconds <= 60 and len(hashed) == before
    # an unknown name costs the same hash as a known one: six attempts, six hashes
    assert len(hashed) == 6 and all(h.startswith("scrypt$16$8$1$") for h in hashed)
    # after the window the right password works, and clears the count
    assert attempt("rph", PASSWORD, 1100.0)["role"] == "pharmacist"
    assert conn.execute("SELECT COUNT(*) FROM attempt").fetchone()[0] == 0


def tsessionsendonsignoutandonexpiry(conn):
    patients.add_account(conn, "rph", PASSWORD, "pharmacist", 16)
    acct = patients.sign_in(conn, "rph", PASSWORD, 16, 5, 60.0)
    jti = patients.open_session(conn, acct["id"], ttl=100, now=1000.0)
    assert patients.session(conn, jti, now=1050.0)["username"] == "rph"
    assert patients.session(conn, jti, now=1100.5) is None          # expired
    assert patients.session(conn, "no-such-session", now=1050.0) is None
    patients.close_session(conn, jti)
    assert patients.session(conn, jti, now=1050.0) is None          # signed out


def tchangingapasswordneedsthecurrentoneandclearstheflag(conn):
    row = patients.clean({"name": "Ada Example", "dob": "1961-04-09", "drugs": "lisinopril"})[0]
    pid = patients.add_patient(conn, row, "rph")
    name, once = patients.set_login(conn, pid, "ada", 16)
    assert name == "ada" and len(once) >= patients.MIN_PASSWORD
    assert patients.sign_in(conn, "ada", once, 16, 5, 60.0)["must_change"] == 1
    with pytest.raises(ValueError, match="current password is wrong"):
        patients.change_password(conn, "ada", "not the one-time one", PASSWORD, 16)
    with pytest.raises(ValueError, match="must differ"):
        patients.change_password(conn, "ada", once, once, 16)
    with pytest.raises(ValueError, match="at least 12"):
        patients.change_password(conn, "ada", once, "short", 16)
    patients.change_password(conn, "ada", once, PASSWORD, 16)
    acct = patients.sign_in(conn, "ada", PASSWORD, 16, 5, 60.0)
    assert acct["must_change"] == 0 and acct["patient"] == pid

    # asking again resets the login and signs the patient out everywhere
    jti = patients.open_session(conn, acct["id"], ttl=100)
    again, fresh = patients.set_login(conn, pid, "another-name", 16)
    assert again == "ada" and fresh != once
    assert patients.session(conn, jti) is None
    assert patients.sign_in(conn, "ada", fresh, 16, 5, 60.0)["must_change"] == 1


# ── the audit trail and the other append-only tables ────────────────────────

def tauditreadingsanddecisionsareappendonly(conn):
    row = patients.clean({"name": "Ada Example", "dob": "1961-04-09", "drugs": "lisinopril",
                          "egfr": "54"})[0]
    pid = patients.add_patient(conn, row, "rph")
    patients.audit(conn, "rph", "pharmacist", "GET /patients/{pid}", True, "req-1", pid)
    conn.execute("INSERT INTO decision (patient, item, verdict, by, at) VALUES (?,?,?,?,?)",
                 (pid, "item-1", "accepted", "rph", "2026-11-16"))
    conn.commit()
    for table in patients.APPEND_ONLY:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
        for change in (f"UPDATE {table} SET id = id + 100", f"DELETE FROM {table}"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(change)
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    who, action, patient, request_id, allowed = conn.execute(
        "SELECT who, action, patient, request_id, allowed FROM audit").fetchone()
    assert (who, action, patient, request_id, allowed) == (
        "rph", "GET /patients/{pid}", pid, "req-1", 1)


# ── validation: one function for the upload and for manual entry ────────────

def tcleankeepsagoodrowandnameseverybadfield():
    row, errors = patients.clean(
        {"name": "  Ada   Example ", "dob": "1961-04-09",
         "drugs": "alphazine; Alphazine ;betamol", "systolic": "128", "diastolic": "82",
         "egfr": "54.0", "pregnant": "No", "allergies": " penicillin ", "taken": "2026-10-01"},
        today=TODAY)
    assert not errors
    assert row == {"name": "Ada Example", "dob": "1961-04-09", "drugs": ["alphazine", "betamol"],
                   "readings": {"systolic": "128", "diastolic": "82", "egfr": "54",
                                "pregnant": "no", "allergies": "penicillin"},
                   "taken": "2026-10-01"}

    _, errors = patients.clean(
        {"name": "", "dob": "2030-01-01", "drugs": " ; ", "weight_kg": "-4", "systolic": "110",
         "diastolic": "120", "potassium": "high", "pregnant": "maybe"}, today=TODAY)
    assert set(errors) == {"name", "dob", "drugs", "weight_kg", "diastolic", "potassium",
                           "pregnant"}
    assert "in the future" in errors["dob"]
    assert errors["weight_kg"] == "-4 is outside 0.5 to 500 kg"       # cannot be right
    assert "is not below systolic 110" in errors["diastolic"]
    assert errors["potassium"] == "'high' is not a number"

    # one number of a blood pressure is not a blood pressure
    _, errors = patients.clean({"name": "Ada Example", "dob": "1961-04-09",
                                "drugs": "alphazine", "systolic": "120"}, today=TODAY)
    assert errors == {"diastolic": "blood pressure needs both numbers"}
    # a reading cannot be dated before the patient was born, or after today
    _, errors = patients.clean({"name": "Ada Example", "dob": "1961-04-09",
                                "drugs": "alphazine", "egfr": "50", "taken": "1960-01-01"},
                               today=TODAY)
    assert "before 1961-04-09" in errors["taken"]
    assert set(patients.BOUNDS) | {"pregnant", "allergies"} == set(patients.INDICATORS)


def tduplicatesarecaughtacrosscasespacingandaccents():
    key = patients.match_key
    same = [key("José Alan", "1980-01-02"), key("JOSE   ALAN", "1980-01-02"),
            key(" jose alan ", "1980-01-02"), key("Jose\u0301 Alan", "1980-01-02")]
    assert len(set(same)) == 1
    assert key("Jose Alan", "1980-01-03") not in same        # another birthday
    assert key("Jose Allan", "1980-01-02") not in same       # another name


# ── upload ───────────────────────────────────────────────────────────────────

CSV = ("name,dob,drugs,egfr,systolic,diastolic,weight_kg,taken\n"
       "Ada Example,1961-04-09,alphazine; unobtainium,54,128,82,71,2026-10-01\n"
       "Ben Sample,1975-02-30,alphazine,,,,,\n"
       "Cleo Testcase,1990-05-05,,,,,,\n"
       "Dev Placeholder,1952-09-23,betamol,,90,120,-3,\n"
       "Esme Mockford,1963-12-25,betamol,61,,,,\n")


def tuploadacceptsgoodrowsandreportseachrejectwithitslineandreason():
    rows, rejected = patients.read_rows(CSV, today=TODAY)
    assert [line for line, _ in rows] == [2, 6]
    assert [line for line, _ in rejected] == [3, 4, 5]
    why = dict(rejected)
    assert why[3] == "dob: the date of birth must be a date written YYYY-MM-DD"
    assert why[4] == "drugs: at least one drug is required"
    assert "weight_kg: -3 is outside 0.5 to 500 kg" in why[5] and "diastolic: " in why[5]
    # a header without the required columns rejects the file, on line 1
    assert patients.read_rows("name,drugs\nAda,alphazine\n") == (
        [], [(1, "the header row needs these columns: dob")])
    assert patients.read_rows("")[1][0][0] == 1
    # a row longer than the header is a shifted row, not extra data to ignore
    shifted = patients.read_rows("name,dob,drugs\nAda Example,1961-04-09,alphazine,54\n")
    assert shifted == ([], [(2, "more cells than the header has columns")])


def tasecondupladupdatesthepatientinsteadofduplicatingthem(conn):
    rows, _ = patients.read_rows(CSV, today=TODAY)
    first = patients.load(conn, rows, "rph")
    assert (len(first["created"]), len(first["updated"])) == (2, 0)
    readings = conn.execute("SELECT COUNT(*) FROM reading").fetchone()[0]

    again = patients.load(conn, rows, "rph")                 # the same file once more
    assert (len(again["created"]), len(again["updated"])) == (0, 2)
    assert conn.execute("SELECT COUNT(*) FROM patient").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM reading").fetchone()[0] == readings

    later = ("name,dob,drugs,egfr,taken\n"
             " ADA  example ,1961-04-09,Alphazine; betamol,47,2026-11-01\n")
    rows, _ = patients.read_rows(later, today=TODAY)
    third = patients.load(conn, rows, "amy")
    ada = first["created"][0]
    assert third == {"created": [], "updated": [ada], "rejected": []}
    profile = patients.profile(conn, ada)
    assert [d["name"] for d in profile["drugs"]] == ["alphazine", "unobtainium", "betamol"]
    assert profile["current"]["egfr"] | {"entered": ""} == {
        "indicator": "egfr", "value": "47", "taken": "2026-11-01", "entered": "",
        "entered_by": "amy"}
    assert [h["value"] for h in profile["history"] if h["indicator"] == "egfr"] == ["47", "54"]


def ttworecordswiththesamenameandbirthdayarenotguessedbetween(conn):
    row = patients.clean({"name": "Ada Example", "dob": "1961-04-09", "drugs": "alphazine"})[0]
    patients.add_patient(conn, row, "rph")
    patients.add_patient(conn, row, "rph")                   # confirmed by hand as a second person
    result = patients.load(conn, [(2, row)], "rph")
    assert result["created"] == result["updated"] == []
    line, why = result["rejected"][0]
    assert line == 2 and "matches 2 existing patients" in why


def thistorykeepseveryreadingandacorrectionisanewone(conn):
    row = patients.clean({"name": "Ada Example", "dob": "1961-04-09", "drugs": "alphazine",
                          "egfr": "54", "taken": "2026-10-01"}, today=TODAY)[0]
    pid = patients.add_patient(conn, row, "rph")
    patients.add_readings(conn, pid, {"egfr": "45"}, "2026-10-01", "amy")    # a correction
    patients.add_readings(conn, pid, {"egfr": "60"}, "2026-09-01", "rph")    # an older result
    profile = patients.profile(conn, pid)
    # current is the newest reading; for one day, the one entered last
    assert (profile["current"]["egfr"]["value"], profile["current"]["egfr"]["entered_by"]) == (
        "45", "amy")
    assert [(h["value"], h["taken"], h["entered_by"]) for h in profile["history"]] == [
        ("45", "2026-10-01", "amy"), ("54", "2026-10-01", "rph"), ("60", "2026-09-01", "rph")]
    # stopping a drug keeps the row
    drug = profile["drugs"][0]["id"]
    assert patients.stop_drug(conn, pid, drug) and not patients.stop_drug(conn, pid, drug)
    assert patients.profile(conn, pid)["drugs"] == []
    assert conn.execute("SELECT stopped FROM drug WHERE id = ?", (drug,)).fetchone()[0]


# ── which label a typed name means ───────────────────────────────────────────

def tatypednamematchesonedrugoritisnotguessed():
    c = PATIENT_CORPUS
    assert set(review.drugs(c)) == {"alphazine", "betamol", "gammavir", "deltaquin"}
    assert [e["doc_id"] for e in review.drugs(c)["gammavir"].labels] == ["gamma", "gamma-er"]
    assert review.labeled(c)["gammavir"] == "gammavir sodium"
    assert review.match("Betamol", c) == "betamol"
    assert review.match("gammavir sodium extended-release", c) == "gammavir"
    assert review.match("Gammavir ER 500 mg", c) == "gammavir"
    assert review.match("22222222201", c) == "betamol"               # an NDC on the label
    assert review.match("deltaquin and betamol", c) == "deltaquin"   # the whole name wins
    assert review.match("betamol with gammavir", c) is None          # two drugs: not guessed
    assert review.match("unobtainium", c) is None
    tagged = review.tag([{"name": "gammavir"}, {"name": "unobtainium"}], c)
    assert [(t["drug"], t["label"]) for t in tagged] == [
        ("gammavir", "GAMMAVIR, GAMMAVIR ER"), (None, None)]


def real_corpus() -> dict:
    return json.loads((ROOT / "corpus.json").read_text(encoding="utf-8"))


def ttherealcorpusis45drugsandeverysuggestednamematchesone():
    corpus = real_corpus()
    assert len(corpus["inserts"]) == 50 and len(review.drugs(corpus)) == 45
    for name in review.suggestions(corpus):
        assert review.match(name, corpus), name


# ── mock patients ────────────────────────────────────────────────────────────

def tmockpatientsarethesamebytesforthesameseed():
    labeled = review.labeled(real_corpus())
    one, two = patients.mock_csv(labeled), patients.mock_csv(labeled)
    assert one == two and one != patients.mock_csv(labeled, seed=8)
    assert one.splitlines()[0] == ",".join(patients.COLUMNS)


def tmockpatientscovereachpathandthekeypairs():
    corpus = real_corpus()
    rows, rejected = patients.read_rows(patients.mock_csv(review.labeled(corpus)), today=TODAY)
    assert len(rows) == 25 and not rejected
    names = [row["name"] for _, row in rows]
    assert len(set(names)) == 25
    assert all(n.split()[-1] in patients._LAST for n in names)           # plainly fictional
    taken = []
    for _, row in rows:
        keys = [review.match(d, corpus) for d in row["drugs"]]
        assert 2 <= len(keys) <= 6 and None not in keys and len(set(keys)) == len(keys)
        taken.append(set(keys))
    for pair in patients.KEY_PAIRS:                                      # the answer key's pairs
        assert any(set(pair) <= mine for mine in taken), pair
    seen = [row["readings"] for _, row in rows]
    assert float(seen[0]["egfr"]) < 60                                   # low kidney function
    assert float(seen[1]["alt"]) > 40 and float(seen[1]["ast"]) > 40     # raised liver enzymes
    assert seen[2]["pregnant"] == "yes"
    plain = seen[3]                                                      # nothing notable
    assert float(plain["egfr"]) >= 60 and float(plain["alt"]) <= 40 and "pregnant" not in plain
    # a small corpus still yields patients, with as many drugs as it has
    rows, rejected = patients.read_rows(patients.mock_csv(review.labeled(PATIENT_CORPUS)),
                                        today=TODAY)
    assert len(rows) == 25 and not rejected


def tthetemplateisavalidupload():
    text = patients.template()
    assert text.splitlines()[0] == ",".join(patients.COLUMNS)
    rows, rejected = patients.read_rows(text, today=TODAY)
    assert len(rows) == 1 and not rejected
