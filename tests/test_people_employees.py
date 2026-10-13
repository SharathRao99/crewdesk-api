"""Employees: validation, uniqueness, reporting lines, search, access and isolation."""

import pytest

from tests.conftest import KEY

BASE = {"name": "Asha Rao", "work_email": "asha@acme.com", "joining_date": "2024-04-01"}


def H(tenant, user):
    return {"X-Service-Key": KEY, "X-Tenant": tenant, "X-User-Id": str(user["_id"])}


@pytest.fixture
def hr(two_tenants):
    from sourcer.tenancy import registry

    return {slug: registry.get_user_by_id(v["user_id"]) for slug, v in two_tenants.items()}


@pytest.fixture
def staff(two_tenants, hr):
    """A recruiter and an employee in acme, both past their one-time password."""
    from sourcer.tenancy import registry

    out = {}
    for role in ("recruiter", "employee"):
        user, first = registry.create_user(f"{role}@acme.com", "acme", role)
        registry.change_password(user["email"], first, "a-long-enough-password")
        out[role] = registry.get_user_by_id(user["_id"])
    return out


def add(api, hr, **over):
    res = api.post("/people/employees", json={**BASE, **over}, headers=H("acme", hr["acme"]))
    return res


def test_create_get_and_defaults(api, hr):
    res = add(api, hr)
    assert res.status_code == 201
    e = res.json()
    assert e["employee_no"] == "EMP-0001" and e["status"] == "active" and e["has_login"] is False
    assert e["work_email"] == "asha@acme.com" and e["manager_id"] is None
    assert api.get(f"/people/employees/{e['id']}", headers=H("acme", hr["acme"])).json() == e
    assert add(api, hr, name="Ben", work_email="ben@acme.com").json()["employee_no"] == "EMP-0002"


def test_email_is_normalised_and_unique_ignoring_case(api, hr):
    assert add(api, hr, work_email="  ASHA@Acme.com ").json()["work_email"] == "asha@acme.com"
    dup = add(api, hr, name="Other", work_email="asha@ACME.com")
    assert dup.status_code == 422 and "work email already exists" in dup.json()["detail"]


def test_employee_number_manual_unique_and_not_clashing_with_auto(api, hr):
    first = add(api, hr, employee_no="emp-0002").json()
    assert first["employee_no"] == "EMP-0002"  # upper-cased
    assert add(api, hr, name="B", work_email="b@acme.com", employee_no="EMP-0002").status_code == 422
    auto1 = add(api, hr, name="C", work_email="c@acme.com").json()["employee_no"]
    auto2 = add(api, hr, name="D", work_email="d@acme.com").json()["employee_no"]
    assert auto1 == "EMP-0001" and auto2 == "EMP-0003"  # the counter skips the one typed by hand


@pytest.mark.parametrize("bad", [
    {"name": ""}, {"name": "   "}, {"name": "x" * 101}, {"name": "bad\x00name"},
    {"work_email": "nope"}, {"work_email": "a@b"}, {"work_email": "x" * 250 + "@b.co"},
    {"joining_date": "2024-02-30"}, {"joining_date": "24-01-01"}, {"joining_date": "1960-01-01"}, {"joining_date": "2999-01-01"}, {"joining_date": ""},
    {"phone": "abc"}, {"phone": "1234"}, {"phone": "1" * 21},
    {"employee_no": "has space"}, {"employee_no": "x" * 21},
    {"designation": "x" * 81}, {"department": "x" * 81},
    {"manager_id": "not-an-id"}, {"manager_id": "a" * 24},
])
def test_rejects_bad_input(api, hr, bad):
    res = add(api, hr, **bad)
    assert res.status_code == 422, (bad, res.text)
    assert api.get("/people/employees", headers=H("acme", hr["acme"])).json()["total"] == 0  # nothing half-saved


def test_unknown_fields_and_missing_fields_are_refused(api, hr):
    h = H("acme", hr["acme"])
    assert api.post("/people/employees", json={**BASE, "status": "inactive"}, headers=h).status_code == 422  # not settable on create
    assert api.post("/people/employees", json={**BASE, "salary": 1}, headers=h).status_code == 422
    assert api.post("/people/employees", json={"name": "x"}, headers=h).status_code == 422


def test_update_changes_only_what_is_sent_and_records_field_names(api, hr):
    from sourcer.tenancy.db import tenant_db

    e = add(api, hr, designation="Engineer").json()
    h = H("acme", hr["acme"])
    res = api.patch(f"/people/employees/{e['id']}", json={"phone": "+91 98765 43210", "designation": None}, headers=h)
    assert res.status_code == 200
    out = res.json()
    assert out["phone"] == "+91 98765 43210" and out["designation"] is None and out["name"] == "Asha Rao"
    log = list(tenant_db("acme")["audit_log"].find({"action": "employee_updated"}))
    assert sorted(log[0]["detail"]["fields"]) == ["designation", "phone"]
    assert "98765" not in str(log) and "Engineer" not in str(log)  # personal values never go in the log
    assert api.patch(f"/people/employees/{e['id']}", json={"name": None}, headers=h).status_code == 422
    assert api.patch(f"/people/employees/{e['id']}", json={}, headers=h).status_code == 200  # nothing to change
    assert not list(tenant_db("acme")["audit_log"].find({"action": "employee_updated", "detail.fields": []}))


def test_update_cannot_take_an_existing_email_or_number(api, hr):
    a = add(api, hr).json()
    b = add(api, hr, name="Ben", work_email="ben@acme.com").json()
    h = H("acme", hr["acme"])
    assert api.patch(f"/people/employees/{b['id']}", json={"work_email": a["work_email"]}, headers=h).status_code == 422
    assert api.patch(f"/people/employees/{b['id']}", json={"employee_no": a["employee_no"]}, headers=h).status_code == 422
    assert api.patch(f"/people/employees/{b['id']}", json={"employee_no": ""}, headers=h).status_code == 422


def test_reporting_lines(api, hr):
    h = H("acme", hr["acme"])
    boss = add(api, hr).json()
    ben = add(api, hr, name="Ben", work_email="ben@acme.com", manager_id=boss["id"]).json()
    assert ben["manager_id"] == boss["id"] and ben["manager_name"] == "Asha Rao"
    cy = add(api, hr, name="Cy", work_email="cy@acme.com", manager_id=ben["id"]).json()

    for mgr in (cy["id"], ben["id"]):  # self, and a loop through the chain
        res = api.patch(f"/people/employees/{boss['id']}" if mgr == cy["id"] else f"/people/employees/{ben['id']}", json={"manager_id": mgr}, headers=h)
        assert res.status_code == 422 and ("loop" in res.json()["detail"])
    assert api.patch(f"/people/employees/{cy['id']}", json={"manager_id": cy["id"]}, headers=h).status_code == 422  # own manager
    assert api.patch(f"/people/employees/{ben['id']}", json={"manager_id": None}, headers=h).json()["manager_id"] is None


def test_cannot_deactivate_someone_who_still_has_reports(api, hr):
    h = H("acme", hr["acme"])
    boss = add(api, hr).json()
    ben = add(api, hr, name="Ben", work_email="ben@acme.com", manager_id=boss["id"]).json()
    blocked = api.patch(f"/people/employees/{boss['id']}", json={"status": "inactive"}, headers=h)
    assert blocked.status_code == 422 and "report to this person" in blocked.json()["detail"]
    api.patch(f"/people/employees/{ben['id']}", json={"manager_id": None}, headers=h)
    assert api.patch(f"/people/employees/{boss['id']}", json={"status": "inactive"}, headers=h).json()["status"] == "inactive"
    # an inactive person cannot be picked as a manager, and drops out of the picker
    assert add(api, hr, name="Di", work_email="di@acme.com", manager_id=boss["id"]).status_code == 422
    assert [o["name"] for o in api.get("/people/employees/options", headers=h).json()] == ["Ben"]
    assert api.patch(f"/people/employees/{ben['id']}", json={"status": "gone"}, headers=h).status_code == 422


def test_search_filter_sort_and_pages(api, hr):
    h = H("acme", hr["acme"])
    for i, (name, dept, date) in enumerate([("Zara", "Sales", "2023-01-10"), ("Adam", "Tech", "2021-05-01"), ("Mia", "Tech", "2022-03-15")]):
        add(api, hr, name=name, work_email=f"{name.lower()}@acme.com", department=dept, joining_date=date)
    api.patch(f"/people/employees/{add(api, hr, name='Old', work_email='old@acme.com').json()['id']}", json={"status": "inactive"}, headers=h)

    names = lambda **q: [e["name"] for e in api.get("/people/employees", params=q, headers=h).json()["items"]]  # noqa: E731
    assert names() == ["Adam", "Mia", "Old", "Zara"]
    assert names(order="desc") == ["Zara", "Old", "Mia", "Adam"]
    assert names(sort="joining_date") == ["Adam", "Mia", "Zara", "Old"]  # Old joined in 2024, the latest
    assert names(status="inactive") == ["Old"] and names(status="active") == ["Adam", "Mia", "Zara"]
    assert names(q="TECH") == ["Adam", "Mia"]  # matches department, any case
    assert names(q="zara@acme") == ["Zara"]
    assert names(q=".*") == []  # a pattern is searched as text, not run
    page = api.get("/people/employees", params={"page_size": 2, "page": 2}, headers=h).json()
    assert page["total"] == 4 and [e["name"] for e in page["items"]] == ["Old", "Zara"]
    for bad in ({"sort": "salary"}, {"order": "sideways"}, {"status": "x"}, {"page": 0}, {"page_size": 0}, {"page_size": 101}, {"q": "x" * 101}):
        assert api.get("/people/employees", params=bad, headers=h).status_code == 422, bad


def test_ids_that_are_not_ids(api, hr):
    h = H("acme", hr["acme"])
    for bad in ("nope", "a" * 24, "1" * 25):
        assert api.get(f"/people/employees/{bad}", headers=h).status_code == 404
        assert api.patch(f"/people/employees/{bad}", json={"name": "x"}, headers=h).status_code == 404


def test_each_company_only_sees_its_own_people(api, hr):
    asha = add(api, hr).json()
    other = H("globex", hr["globex"])
    assert api.get("/people/employees", headers=other).json()["total"] == 0
    assert api.get(f"/people/employees/{asha['id']}", headers=other).status_code == 404
    assert api.patch(f"/people/employees/{asha['id']}", json={"name": "Hacked"}, headers=other).status_code == 404
    # the same email and number can exist in another company
    same = api.post("/people/employees", json=BASE, headers=other)
    assert same.status_code == 201 and same.json()["employee_no"] == "EMP-0001"
    # a manager from another company is not a manager here
    res = api.post("/people/employees", json={**BASE, "work_email": "x@globex.com", "manager_id": asha["id"]}, headers=other)
    assert res.status_code == 422


def test_only_hr_administrators_can_touch_employee_data(api, hr, staff):
    asha = add(api, hr).json()
    for who in ("recruiter", "employee"):
        h = H("acme", staff[who])
        assert api.get("/people/employees", headers=h).status_code == 403, who
        assert api.get(f"/people/employees/{asha['id']}", headers=h).status_code == 403
        assert api.post("/people/employees", json={**BASE, "work_email": "n@acme.com"}, headers=h).status_code == 403
        assert api.patch(f"/people/employees/{asha['id']}", json={"name": "x"}, headers=h).status_code == 403
        assert api.get("/people/employees/options", headers=h).status_code == 403


def test_creating_is_audited_without_personal_details(api, hr):
    from sourcer.tenancy.db import tenant_db

    e = add(api, hr).json()
    log = list(tenant_db("acme")["audit_log"].find({"action": "employee_created"}))
    assert len(log) == 1 and log[0]["detail"] == {"employee": e["id"]}
    assert "asha" not in str(log).lower()


def test_indexes_exist_for_existing_companies_after_ensure(two_tenants):
    from sourcer import admin
    from sourcer.tenancy.db import tenant_db

    admin.main(["indexes", "ensure", "--all"])
    idx = {i["name"]: i for i in tenant_db("acme")["employees"].list_indexes()}
    assert idx["work_email_1"].get("unique") and idx["employee_no_1"].get("unique")
