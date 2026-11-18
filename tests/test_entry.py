# The new-patient bar's side of the server: a patient entered by hand. Required
# fields, an error per field, duplicates caught whatever the case, spacing or
# accents, an unmatched drug carried to the report as "not checked", the audit
# row, and the two anonymous measures the form reports about itself.
import json
from contextlib import closing

import pytest
from conftest import signin

pytest.importorskip("fastapi")

from pharmacli import patients  # noqa: E402


@pytest.fixture
def staff(clinic):
    clinic.headers.update(signin(clinic))
    return clinic


def new(staff, **over):
    body = {"name": "Ada Example", "dob": "1961-04-09", "drugs": ["alphazine"],
            "entry": {"seconds": 41.5, "attempt": 1}} | over
    return staff.post("/patients", json=body)


def listed(staff) -> list:
    return staff.get("/patients").json()["patients"]


def tanameabirthdayandadrugarerequiredandeachfieldgetsitsownerror(staff):
    r = staff.post("/patients", json={"name": " ", "dob": "", "drugs": [], "weight_kg": "-2"})
    assert r.status_code == 422
    assert r.json()["detail"]["errors"] == {
        "name": "a name is required", "dob": "a date of birth is required",
        "drugs": "at least one drug is required", "weight_kg": "-2 is outside 0.5 to 500 kg"}
    assert listed(staff) == []                               # nothing was half-saved

    saved = new(staff)                                       # the indicators are optional
    assert saved.status_code == 201
    profile = saved.json()
    assert (profile["name"], profile["dob"]) == ("Ada Example", "1961-04-09")
    assert profile["current"] == {}
    assert [(d["name"], d["label"]) for d in profile["drugs"]] == [("alphazine", "ALPHAZINE")]
    with_readings = new(staff, name="Ben Sample", systolic="128", diastolic="82", egfr="54")
    assert with_readings.json()["current"]["egfr"]["entered_by"] == "rph"
    # the drugs may arrive as a list or as one string separated by semicolons
    assert len(new(staff, name="Cleo Testcase", drugs="alphazine; betamol").json()["drugs"]) == 2


def taduplicateiscaughtwhateverthecasespacingoraccents(staff):
    assert new(staff, name="José Alán").status_code == 201
    for variant in ("jose alan", "JOSE   ALAN", "  José  Alán ", "José Alán"):
        r = new(staff, name=variant)
        assert r.status_code == 409, variant
        detail = r.json()["detail"]
        assert detail["duplicate"] == 1 and "already on file" in detail["message"]
        assert "Jos" not in json.dumps(detail)               # it says one exists, not who
    assert len(listed(staff)) == 1                           # the server refused, not the page
    # another birthday is another person
    assert new(staff, name="jose alan", dob="1961-04-10").status_code == 201
    # and a pharmacist can confirm a second record for the same name and birthday
    assert new(staff, name="jose alan", confirm=True).status_code == 201
    assert len(listed(staff)) == 3
    assert new(staff, name="jose alan", confirm="yes").status_code == 409    # only a real true


def tanunmatcheddrugisflaggedatentryandreportedasnotchecked(staff):
    saved = new(staff, drugs=["alphazine", "Unobtainium 5 mg"]).json()
    assert [(d["name"], d["drug"], d["label"]) for d in saved["drugs"]] == [
        ("alphazine", "alphazine", "ALPHAZINE"), ("Unobtainium 5 mg", None, None)]
    rep = staff.get(f"/patients/{saved['id']}/report").json()
    assert rep["summary"] == "0 of 1 pairs checked; not checked, no label: Unobtainium 5 mg"
    assert [(i["status"], i["title"]) for i in rep["items"] if i["kind"] == "pair"] == [
        ("Not checked", "alphazine hydrochloride + Unobtainium 5 mg")]
    # what the page mirrors the match with, so the flag shows as the name is typed
    listing = staff.get("/patients").json()
    assert listing["words"] == {"alphazine": "alphazine", "betamol": "betamol",
                                "gammavir": "gammavir", "deltaquin": "deltaquin"}
    assert {"alphazine hydrochloride", "deltaquin plus"} <= set(listing["suggest"])


def tsavingisauditedandtheformstwomeasurescarrynothingaboutthepatient(staff):
    def measures():
        got = staff.get("/metrics").json()["new_patient"]
        return (got.get("first_submit_valid", 0), got.get("first_submit_invalid", 0),
                got["seconds"]["n"])

    before = measures()
    assert new(staff, name="", entry={"seconds": 12.0, "attempt": 1}).status_code == 422
    assert new(staff, entry={"seconds": 95.5, "attempt": 2}).status_code == 201   # fixed on the 2nd
    assert new(staff, name="Ben Sample", entry={"seconds": 41.5, "attempt": 1}).status_code == 201
    assert new(staff, name="Cleo Testcase", entry="junk").status_code == 201      # no measure sent
    after = measures()
    assert [b - a for a, b in zip(before, after, strict=True)] == [1, 1, 2]
    got = staff.get("/metrics").json()
    assert got["new_patient"]["seconds"]["p50"] in (41.5, 95.5)
    assert "POST /patients" in got["requests"]
    text = json.dumps(got)
    for secret in ("Ada", "Example", "Ben Sample", "1961-04-09", "alphazine"):
        assert secret not in text, secret
    with closing(patients.connect(staff.db)) as conn:
        rows = [tuple(r) for r in conn.execute(
            "SELECT who, role, allowed FROM audit WHERE action = 'POST /patients'")]
        made = [r["created_by"] for r in conn.execute("SELECT created_by FROM patient")]
    assert rows == [("rph", "pharmacist", 1)] * 4 and made == ["rph"] * 3


def tapharmacistcreatesthepatientsloginwithapasswordthatworksonce(staff):
    pid = new(staff).json()["id"]
    assert staff.post(f"/patients/{pid}/login", json={"username": "a b"}).json()["detail"] == {
        "errors": {"username": "a username is 3 to 32 letters, digits, dots, dashes or "
                               "underscores"}}
    made = staff.post(f"/patients/{pid}/login", json={"username": "Ada.E"}).json()
    assert made["username"] == "ada.e" and len(made["one_time_password"]) >= 12
    assert staff.get(f"/patients/{pid}").json()["login"] == "ada.e"
    first = staff.post("/auth/login", headers={"Authorization": ""},
                       json={"username": "ada.e", "password": made["one_time_password"]}).json()
    assert first["role"] == "patient" and first["must_change"] is True
    # asking again replaces the password; the old one stops working
    again = staff.post(f"/patients/{pid}/login", json={"username": "ignored"}).json()
    assert again["username"] == "ada.e" and again["one_time_password"] != made["one_time_password"]
    assert staff.post("/auth/login", headers={"Authorization": ""}, json={
        "username": "ada.e", "password": made["one_time_password"]}).status_code == 401
    assert staff.post(f"/patients/{'0' * 16}/login", json={"username": "nobody"}).status_code == 404
