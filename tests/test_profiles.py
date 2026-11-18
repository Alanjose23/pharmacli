# Profiles through the API, as a signed-in pharmacist: upload with its reject
# report, the mock patients, a profile with its history, edits that overwrite
# nothing, and the audit row every one of those leaves.
from contextlib import closing

import pytest
from conftest import signin

pytest.importorskip("fastapi")

from test_patients import CSV  # noqa: E402

from pharmacli import patients, serve  # noqa: E402


@pytest.fixture
def staff(clinic):
    clinic.headers.update(signin(clinic))
    return clinic


def ids(client) -> dict:
    return {p["name"]: p["id"] for p in client.get("/patients").json()["patients"]}


def tuploadstoresgoodrowsandreturnseachrejectwithitsline(staff):
    first = staff.post("/patients/upload", content=CSV.encode()).json()
    assert (first["created"], first["updated"]) == (2, 0)
    assert [r["line"] for r in first["rejected"]] == [3, 4, 5]
    assert first["rejected"][1] == {"line": 4, "reason": "drugs: at least one drug is required"}
    again = staff.post("/patients/upload", content=CSV.encode()).json()
    assert (again["created"], again["updated"]) == (0, 2)        # updated, not created
    listing = staff.get("/patients").json()
    assert [p["name"] for p in listing["patients"]] == ["Ada Example", "Esme Mockford"]
    assert listing["patients"][0]["drugs"] == 2
    # what the page needs to mirror the server's checks
    assert "betamol" in listing["suggest"] and listing["labeled"]["gammavir"] == "gammavir sodium"
    assert listing["bounds"]["weight_kg"] == {"unit": "kg", "low": 0.5, "high": 500}


def tanuploadoverthecaporinthewrongencodingisrefused(staff, monkeypatch):
    assert staff.post("/patients/upload", content=b"\xff\xfe\x00").status_code == 422
    monkeypatch.setattr(serve, "MAX_UPLOAD", 10)
    r = staff.post("/patients/upload", content=CSV.encode())
    assert r.status_code == 413 and "larger than" in r.json()["detail"]
    assert staff.get("/patients").json()["patients"] == []


def taprofileflagsanunmatcheddrugandkeepsit(staff):
    staff.post("/patients/upload", content=CSV.encode())
    profile = staff.get(f"/patients/{ids(staff)['Ada Example']}").json()
    assert (profile["name"], profile["dob"]) == ("Ada Example", "1961-04-09")
    assert profile["login"] is None
    # carried through, not dropped and not guessed
    assert [(d["name"], d["drug"], d["label"]) for d in profile["drugs"]] == [
        ("alphazine", "alphazine", "ALPHAZINE"), ("unobtainium", None, None)]
    assert profile["current"]["egfr"]["value"] == "54"
    assert profile["current"]["systolic"]["taken"] == "2026-10-01"
    assert staff.get("/patients/0000000000000000").status_code == 404


def teditsaddtothehistoryandoverwritenothing(staff):
    staff.post("/patients/upload", content=CSV.encode())
    pid = ids(staff)["Ada Example"]
    after = staff.post(f"/patients/{pid}/readings", json={"egfr": "47"}).json()
    now = after["current"]["egfr"]
    assert (now["value"], now["entered_by"]) == ("47", "rph")
    assert [h["value"] for h in after["history"] if h["indicator"] == "egfr"] == ["47", "54"]

    bad = staff.post(f"/patients/{pid}/readings", json={"egfr": "900", "diastolic": "70"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["errors"] == {
        "egfr": "900 is outside 0 to 200 mL/min/1.73 m2",
        "systolic": "blood pressure needs both numbers"}
    empty = staff.post(f"/patients/{pid}/readings", json={})
    assert empty.json()["detail"]["errors"] == {"readings": "nothing to add"}

    added = staff.post(f"/patients/{pid}/drugs", json={"name": "  Betamol "}).json()
    assert [(d["name"], d["label"]) for d in added["drugs"]][-1] == ("Betamol", "BETAMOL")
    assert staff.post(f"/patients/{pid}/drugs", json={"name": ""}).status_code == 422
    first = added["drugs"][0]["id"]
    stopped = staff.delete(f"/patients/{pid}/drugs/{first}").json()
    assert [d["name"] for d in stopped["drugs"]] == ["unobtainium", "Betamol"]
    assert staff.delete(f"/patients/{pid}/drugs/{first}").status_code == 404
    # nothing was removed from the history by any of that
    final = staff.get(f"/patients/{pid}").json()
    assert len(final["history"]) == len(after["history"])
    for path in (f"/patients/{'0' * 16}/readings", f"/patients/{'0' * 16}/drugs"):
        assert staff.post(path, json={"egfr": "50", "name": "betamol"}).status_code == 404


def tthemockpatientsloadthroughtheuploadpath(staff):
    assert staff.post("/patients/mock").json() == {"created": 25, "updated": 0, "rejected": []}
    assert staff.post("/patients/mock").json() == {"created": 0, "updated": 25, "rejected": []}
    assert len(staff.get("/patients").json()["patients"]) == 25


def tthetemplateisservedwiththeuploadscolumns(staff):
    r = staff.get("/static/template.csv", headers={"Authorization": ""})      # nothing secret in it
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0] == ",".join(patients.COLUMNS)


def teveryreadandchangeleavesanauditrow(staff):
    staff.post("/patients/upload", content=CSV.encode())
    pid = ids(staff)["Ada Example"]
    staff.get(f"/patients/{pid}")
    staff.post(f"/patients/{pid}/readings", json={"egfr": "47"})
    drug = staff.post(f"/patients/{pid}/drugs", json={"name": "betamol"}).json()["drugs"][-1]["id"]
    staff.delete(f"/patients/{pid}/drugs/{drug}")
    staff.post("/patients/mock")
    with closing(patients.connect(staff.db)) as conn:
        rows = [tuple(r) for r in conn.execute(
            "SELECT action, patient, who, role, allowed FROM audit WHERE action != 'sign-in'"
            " ORDER BY id")]
    assert rows == [
        ("POST /patients/upload", None, "rph", "pharmacist", 1),
        ("GET /patients", None, "rph", "pharmacist", 1),
        ("GET /patients/{pid}", pid, "rph", "pharmacist", 1),
        ("POST /patients/{pid}/readings", pid, "rph", "pharmacist", 1),
        ("POST /patients/{pid}/drugs", pid, "rph", "pharmacist", 1),
        ("DELETE /patients/{pid}/drugs/{drug}", pid, "rph", "pharmacist", 1),
        ("POST /patients/mock", None, "rph", "pharmacist", 1)]
