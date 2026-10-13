"""Employee logins: the one-time link, signing in, seeing only your own data, and erasing a leaver."""

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import KEY

PASSWORD = "a-long-enough-password"
PUB = {"X-Service-Key": KEY, "X-Client-IP": "203.0.113.7"}


def H(tenant, user, version=None):
    h = {"X-Service-Key": KEY, "X-Tenant": tenant, "X-User-Id": str(user["_id"])}
    if version is not None:
        h["X-Session-Version"] = str(version)
    return h


@pytest.fixture
def hr(two_tenants):
    from sourcer.tenancy import registry

    return {slug: registry.get_user_by_id(v["user_id"]) for slug, v in two_tenants.items()}


@pytest.fixture
def c(api, hr):
    class Client:
        def __init__(self, tenant):
            self.tenant, self.h = tenant, H(tenant, hr[tenant])

        def get(self, path, **kw):
            return api.get(path, headers=self.h, **kw)

        def post(self, path, json=None):
            return api.post(path, json=json or {}, headers=self.h)

        def patch(self, path, json=None):
            return api.patch(path, json=json or {}, headers=self.h)

    out = Client("acme")
    out.other = Client("globex")
    return out


def emp(c, name="Asha", **over):
    res = c.post("/people/employees", {"name": name, "work_email": f"{name.lower()}@{c.tenant}.com", "joining_date": "2024-01-15", **over})
    assert res.status_code == 201, res.text
    return res.json()


def link(c, e):
    res = c.post(f"/people/employees/{e['id']}/login-link")
    assert res.status_code == 201, res.text
    return res.json()


def join(api, token, password=PASSWORD):
    return api.post(f"/public/join/{token}", json={"password": password}, headers=PUB)


def member(api, c, name="Asha"):
    """An employee with a working login: returns (employee, user dict as the API sees it, session headers)."""
    e = emp(c, name)
    token = link(c, e)["token"]
    user = join(api, token).json()
    return e, user, {"X-Service-Key": KEY, "X-Tenant": c.tenant, "X-User-Id": user["id"], "X-Session-Version": str(user["session_version"])}


def types(c):
    return {t["code"]: t for t in c.get("/people/leave-types").json()}


def take(c, e, start, end=None, code="PL"):
    res = c.post("/people/leave", {"employee_id": e["id"], "type_id": types(c)[code]["id"], "start_date": start, "end_date": end})
    assert res.status_code == 201, res.text
    return res.json()


# ---------- the link ----------

def test_creating_a_link_makes_a_login_with_no_password_yet(api, c):
    from sourcer.tenancy.db import platform_db

    e = emp(c)
    assert e["has_login"] is False
    made = link(c, e)
    assert len(made["token"]) == 43 and made["email"] == "asha@acme.com" and made["name"] == "Asha"
    user = platform_db()["users"].find_one({"email": "asha@acme.com"})
    assert user["role"] == "employee" and user["employee_id"] == e["id"] and user["password_hash"] is None and user["tenant"] == "acme"
    after = c.get(f"/people/employees/{e['id']}").json()
    assert after["has_login"] is True and after["login"]["state"] == "invited"
    exp = datetime.fromisoformat(made["expires_at"].replace("Z", "+00:00"))
    assert timedelta(days=2, hours=23) < exp - datetime.now(timezone.utc) < timedelta(days=3, hours=1)


def test_only_a_hash_of_the_token_is_stored(api, c):
    from sourcer.tenancy.db import client, platform_db

    token = link(c, emp(c))["token"]
    for name in client().list_database_names():
        if name.startswith(("test_",)) and (name == platform_db().name or "_t_" in name):
            db = client()[name]
            for col in db.list_collection_names():
                assert token not in str(list(db[col].find())), (name, col)
    assert platform_db()["join_links"].count_documents({"token_hash": {"$exists": True}}) == 1


def test_only_hr_administrators_make_links_and_other_companies_cannot(api, c, hr):
    from sourcer.tenancy import registry

    e = emp(c)
    for role in ("recruiter", "employee"):
        user, first = registry.create_user(f"{role}@acme.com", "acme", role)
        registry.change_password(user["email"], first, PASSWORD)
        user = registry.get_user_by_id(user["_id"])
        assert api.post(f"/people/employees/{e['id']}/login-link", headers=H("acme", user)).status_code == 403
        assert api.post(f"/people/employees/{e['id']}/login/disable", headers=H("acme", user)).status_code == 403
    assert c.other.post(f"/people/employees/{e['id']}/login-link").status_code == 404
    assert api.post(f"/people/employees/{e['id']}/login-link", headers={"X-Service-Key": KEY}).status_code == 400


def test_an_email_that_already_has_a_login_cannot_be_given_another(api, c, hr):
    from sourcer.tenancy import registry

    registry.create_user("asha@acme.com", "acme", "recruiter")
    res = c.post(f"/people/employees/{emp(c)['id']}/login-link")
    assert res.status_code == 422 and "already has a login" in res.json()["detail"]


def test_only_active_employees_get_a_link(c):
    e = emp(c)
    c.patch(f"/people/employees/{e['id']}", {"status": "inactive"})
    assert c.post(f"/people/employees/{e['id']}/login-link").status_code == 422
    assert c.post("/people/employees/nope/login-link").status_code == 404


# ---------- setting up from the link ----------

def test_the_page_can_ask_who_a_link_is_for_and_every_bad_link_looks_alike(api, c):
    token = link(c, emp(c))["token"]
    ok = api.get(f"/public/join/{token}", headers=PUB).json()
    assert ok == {"state": "open", "name": "Asha", "company": "Acme Inc"}
    for bad in ("short", "x" * 43, token[:-1] + ("A" if token[-1] != "A" else "B"), "../etc/passwd", "a" * 300):
        res = api.get(f"/public/join/{bad}", headers=PUB)
        assert res.status_code in (200, 404) and (res.status_code == 404 or res.json() == {"state": "invalid", "name": None, "company": None}), bad


def test_choosing_a_password_signs_them_in_and_uses_the_link_up(api, c):
    e = emp(c)
    token = link(c, e)["token"]
    res = join(api, token)
    assert res.status_code == 200
    user = res.json()
    assert user["role"] == "employee" and user["modules"] == ["people"] and user["email"] == "asha@acme.com" and user["tenant_name"] == "Acme Inc"
    login = api.post("/auth/login", json={"email": "asha@acme.com", "password": PASSWORD}, headers=PUB)
    assert login.status_code == 200 and login.json()["id"] == user["id"]
    assert c.get(f"/people/employees/{e['id']}").json()["login"]["state"] == "active"
    assert join(api, token).status_code == 410  # the link is spent
    assert api.get(f"/public/join/{token}", headers=PUB).json()["state"] == "invalid"


def test_a_weak_password_does_not_burn_the_link(api, c):
    token = link(c, emp(c))["token"]
    assert join(api, token, "short").status_code == 422
    assert join(api, token, "x" * 300).status_code == 422
    assert api.post(f"/public/join/{token}", json={"password": PASSWORD, "extra": 1}, headers=PUB).status_code == 422
    assert api.post(f"/public/join/{token}", json={}, headers=PUB).status_code == 422
    assert join(api, token).status_code == 200  # still good


def test_nobody_can_sign_in_before_choosing_a_password(api, c):
    link(c, emp(c))
    for pw in ("", "anything", PASSWORD, None):
        assert api.post("/auth/login", json={"email": "asha@acme.com", "password": pw or ""}, headers=PUB).status_code == 401


def test_an_expired_link_is_dead(api, c):
    from sourcer.tenancy.db import platform_db

    token = link(c, emp(c))["token"]
    platform_db()["join_links"].update_many({}, {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)}})
    assert api.get(f"/public/join/{token}", headers=PUB).json()["state"] == "invalid"
    assert join(api, token).status_code == 410


def test_a_newer_link_replaces_the_old_one_and_ends_sessions(api, c):
    e, user, session = member(api, c)
    assert api.get("/people/me", headers=session).status_code == 200
    first = link(c, e)["token"]
    second = link(c, e)["token"]
    assert join(api, first).status_code == 410  # replaced
    assert api.get("/people/me", headers=session).status_code == 401  # the old session ended when the new link was made
    assert join(api, second, "another-long-password").status_code == 200  # a forgotten password is reset the same way
    assert api.post("/auth/login", json={"email": "asha@acme.com", "password": "another-long-password"}, headers=PUB).status_code == 200
    assert api.post("/auth/login", json={"email": "asha@acme.com", "password": PASSWORD}, headers=PUB).status_code == 401


def test_two_people_racing_for_one_link_only_one_wins(api, c):
    from concurrent.futures import ThreadPoolExecutor

    from sourcer import join as joinmod

    token = link(c, emp(c))["token"]
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda i: joinmod.consume(token, f"password-number-{i}-long"), range(8)))
    assert sum(r is not None for r in results) == 1


def test_guessing_is_throttled(api, c):
    token = link(c, emp(c))["token"]
    codes = [api.post(f"/public/join/{'z' * 43}", json={"password": PASSWORD}, headers={**PUB, "X-Client-IP": f"198.51.100.{i}"}).status_code for i in range(3)]
    assert codes == [410, 410, 410]
    same = [api.post(f"/public/join/{token[:-1]}A", json={"password": PASSWORD}, headers=PUB).status_code for _ in range(12)]
    assert same[:10] == [410] * 10 and 429 in same[10:]  # per link
    ip = {"X-Service-Key": KEY, "X-Client-IP": "192.0.2.99"}
    many = [api.post(f"/public/join/{chr(65 + i % 26) * 43}", json={"password": PASSWORD}, headers=ip).status_code for i in range(24)]
    assert 429 in many  # per address


# ---------- what an employee can do ----------

def test_an_employee_sees_only_their_own_data(api, c):
    a, _, sa = member(api, c, "Asha")
    b, _, sb = member(api, c, "Ben")
    take(c, a, "2026-03-02", "2026-03-04")
    take(c, b, "2026-04-06")
    mine = api.get("/people/me", headers=sa).json()
    assert mine["id"] == a["id"] and mine["name"] == "Asha" and mine["probation"]["status"] in ("review_due", "on_probation", "confirmed")
    bal = api.get("/people/me/balances", params={"year": 2026}, headers=sa).json()
    pl = next(r for r in bal["types"] if r["code"] == "PL")
    assert (pl["used"], pl["remaining"]) == (3, 15)
    leave = api.get("/people/me/leave", headers=sa).json()
    assert leave["total"] == 1 and leave["items"][0]["employee_name"] == "Asha" and leave["items"][0]["days"] == 3
    other = api.get("/people/me/leave", headers=sb).json()
    assert other["total"] == 1 and other["items"][0]["start_date"] == "2026-04-06"  # Ben's, not Asha's
    assert api.get("/people/me/holidays", params={"year": 2026}, headers=sa).status_code == 200
    assert api.get("/people/me/policy", headers=sa).json()["notice_period"]["amount"] == 2
    assert api.get("/people/me/leave", params={"status": "nope"}, headers=sa).status_code == 422
    # an id in the address or the query is simply not a thing here
    assert api.get(f"/people/me/leave?employee_id={b['id']}", headers=sa).json()["items"][0]["employee_name"] == "Asha"


def test_an_employee_is_refused_everything_that_is_not_theirs(api, c):
    a, _, sa = member(api, c, "Asha")
    b = emp(c, "Ben")
    rec = take(c, b, "2026-04-06")
    refused = [
        ("get", "/people/employees"), ("get", f"/people/employees/{b['id']}"), ("patch", f"/people/employees/{b['id']}"), ("get", "/people/employees/options"),
        ("get", "/people/leave"), ("get", f"/people/leave/{rec['id']}"), ("post", "/people/leave"), ("post", f"/people/leave/{rec['id']}/cancel"),
        ("get", f"/people/employees/{b['id']}/balances"), ("get", "/people/policy"), ("patch", "/people/policy"), ("get", "/people/leave-types"),
        ("post", "/people/holidays"), ("post", f"/people/employees/{a['id']}/login-link"), ("post", f"/people/employees/{a['id']}/notice"),
        ("get", "/roles"), ("get", "/candidates"), ("get", "/jobs"), ("get", "/templates"),
    ]
    for method, path in refused:
        res = api.request(method.upper(), path, headers=sa, json={} if method != "get" else None)
        assert res.status_code == 403, (method, path, res.status_code)


def test_people_without_an_employee_record_get_nothing_from_the_self_routes(api, c, hr):
    from sourcer.tenancy import registry

    rec, first = registry.create_user("rec@acme.com", "acme", "recruiter")
    registry.change_password("rec@acme.com", first, PASSWORD)
    for user in (registry.get_user_by_id(rec["_id"]), hr["acme"]):
        for path in ("/people/me", "/people/me/balances", "/people/me/leave", "/people/me/holidays", "/people/me/policy"):
            assert api.get(path, headers=H("acme", user)).status_code == 403, (user["role"], path)
    emp_user, first = registry.create_user("loose@acme.com", "acme", "employee")  # an employee login linked to nothing
    registry.change_password("loose@acme.com", first, PASSWORD)
    assert api.get("/people/me", headers=H("acme", registry.get_user_by_id(emp_user["_id"]))).status_code == 403


def test_an_employee_cannot_use_their_session_against_another_company(api, c):
    _, user, session = member(api, c)
    assert api.get("/people/me", headers={**session, "X-Tenant": "globex"}).status_code == 401


def test_the_employee_can_change_their_own_password_and_avatar(api, c):
    _, user, session = member(api, c)
    assert api.put("/me/avatar", json={"style": "shapes", "seed": "abc"}, headers=session).status_code == 200
    changed = api.post("/me/password", json={"old": PASSWORD, "new": "yet-another-password"}, headers=session)
    assert changed.status_code == 200
    assert api.post("/auth/login", json={"email": "asha@acme.com", "password": "yet-another-password"}, headers=PUB).status_code == 200


# ---------- ending a login ----------

def test_making_someone_inactive_ends_their_login_at_once(api, c):
    e, user, session = member(api, c)
    c.patch(f"/people/employees/{e['id']}", {"status": "inactive"})
    assert api.get("/people/me", headers=session).status_code == 401
    assert api.post("/auth/login", json={"email": "asha@acme.com", "password": PASSWORD}, headers=PUB).status_code == 401
    assert c.get(f"/people/employees/{e['id']}").json()["login"]["state"] == "disabled"
    c.patch(f"/people/employees/{e['id']}", {"status": "active"})  # coming back does not switch it on by itself
    assert api.post("/auth/login", json={"email": "asha@acme.com", "password": PASSWORD}, headers=PUB).status_code == 401
    token = link(c, e)["token"]  # a new link does
    assert join(api, token, "brand-new-long-password").status_code == 200


def test_hr_can_switch_a_login_off(api, c):
    e, user, session = member(api, c)
    off = c.post(f"/people/employees/{e['id']}/login/disable")
    assert off.status_code == 200 and off.json()["login"]["state"] == "disabled"
    assert api.get("/people/me", headers=session).status_code == 401
    assert api.post("/auth/login", json={"email": "asha@acme.com", "password": PASSWORD}, headers=PUB).status_code == 401
    assert c.post(f"/people/employees/{emp(c, 'Ben')['id']}/login/disable").status_code == 422  # nothing to switch off
    # a link for a disabled login does not get through while it is off, but HR making a new one turns it back on
    again = join(api, link(c, e)["token"], "returning-long-password")
    assert again.status_code == 200


def test_a_disabled_login_cannot_use_an_old_link(api, c):
    e = emp(c)
    token = link(c, e)["token"]
    from sourcer.tenancy import registry
    from sourcer.tenancy.db import platform_db

    registry.set_user_disabled(platform_db()["users"].find_one({"email": "asha@acme.com"})["_id"], True)
    assert api.get(f"/public/join/{token}", headers=PUB).json()["state"] == "invalid"
    assert join(api, token).status_code == 410


def test_the_login_follows_the_work_email(api, c, hr):
    from sourcer.tenancy import registry

    e, user, session = member(api, c)
    assert c.patch(f"/people/employees/{e['id']}", {"work_email": "asha.new@acme.com"}).status_code == 200
    assert api.post("/auth/login", json={"email": "asha.new@acme.com", "password": PASSWORD}, headers=PUB).status_code == 200
    assert api.post("/auth/login", json={"email": "asha@acme.com", "password": PASSWORD}, headers=PUB).status_code == 401
    registry.create_user("taken@acme.com", "acme", "recruiter")
    clash = c.patch(f"/people/employees/{e['id']}", {"work_email": "taken@acme.com"})
    assert clash.status_code == 422
    assert c.get(f"/people/employees/{e['id']}").json()["work_email"] == "asha.new@acme.com"  # nothing half-changed


def test_links_and_sign_ins_are_recorded_without_secrets(api, c):
    from sourcer.tenancy.db import platform_db, tenant_db

    e = emp(c)
    token = link(c, e)["token"]
    join(api, "q" * 43)
    join(api, token)
    assert any(a["action"] == "login_link_created" and a["detail"] == {"employee": e["id"]} for a in tenant_db("acme")["audit_log"].find())
    kinds = {x["kind"] for x in platform_db()["auth_events"].find()}
    assert {"join_ok", "join_failed"} <= kinds
    text = str(list(platform_db()["auth_events"].find())) + str(list(tenant_db("acme")["audit_log"].find()))
    assert token not in text and PASSWORD not in text


# ---------- erasing a leaver ----------

def test_anonymise_clears_personal_details_but_keeps_the_numbers(api, c):
    from sourcer import admin
    from sourcer.tenancy.db import platform_db, tenant_db

    boss = emp(c, "Boss")
    e, user, session = member(api, c, "Asha")
    c.patch(f"/people/employees/{e['id']}", {"phone": "+91 98765 43210", "designation": "Engineer"})
    rec = c.post("/people/leave", {"employee_id": e["id"], "type_id": types(c)["PL"]["id"], "start_date": "2026-03-02", "end_date": "2026-03-04", "reason": "Medical", "note": "Private", "approved_by_id": boss["id"]}).json()
    theirs = c.post("/people/leave", {"employee_id": boss["id"], "type_id": types(c)["SL"]["id"], "start_date": "2026-05-04", "approved_by_id": e["id"]}).json()
    c.post(f"/people/employees/{e['id']}/notice", {"given_on": "2026-10-01", "note": "Secret reason"})
    c.post(f"/people/employees/{e['id']}/leave-adjustments", {"type_id": types(c)["PL"]["id"], "year": 2026, "days": 1, "reason": "Personal favour"})
    before = api.get(f"/people/employees/{e['id']}/balances", params={"year": 2026}, headers=c.h).json()

    from sourcer.people import privacy
    from sourcer.people.errors import PeopleError

    db = tenant_db("acme")
    with pytest.raises(PeopleError):
        privacy.anonymise(db, e["id"], "test")  # still active
    c.patch(f"/people/employees/{e['id']}", {"status": "inactive"})
    summary = privacy.anonymise(db, e["id"], "test")
    assert summary == {"leave_records": 1, "adjustments": 1}

    gone = c.get(f"/people/employees/{e['id']}").json()
    assert gone["name"] == "Former employee" and gone["phone"] is None and gone["designation"] is None and gone["work_email"].startswith("anonymised-")
    assert gone["has_login"] is False and gone["notice"]["note"] is None
    assert platform_db()["users"].count_documents({"email": "asha@acme.com"}) == 0
    assert api.post("/auth/login", json={"email": "asha@acme.com", "password": PASSWORD}, headers=PUB).status_code == 401
    kept = c.get(f"/people/leave/{rec['id']}").json()
    assert kept["days"] == 3 and kept["reason"] is None and kept["note"] is None and kept["start_date"] == "2026-03-02"
    assert c.get(f"/people/leave/{theirs['id']}").json()["approved_by_name"] == "Former employee"
    assert api.get(f"/people/employees/{e['id']}/balances", params={"year": 2026}, headers=c.h).json() == before  # the numbers still add up
    text = str(list(db["employees"].find({"_id": __import__("bson").ObjectId(e["id"])}))) + str(list(db["leave_adjustments"].find())) + str(list(db["leave_records"].find({}, {"units": 0})))
    for secret in ("Asha", "Medical", "Private", "Secret reason", "Personal favour", "98765"):
        assert secret not in text, secret
    with pytest.raises(PeopleError):
        privacy.anonymise(db, e["id"], "test")  # only once
    with pytest.raises(SystemExit):
        admin.main(["people", "anonymise", "--tenant", "acme", "--employee", e["id"]])  # needs --confirm


def test_retention_anonymises_only_people_who_left_long_ago(api, c, capsys):
    from bson import ObjectId

    from sourcer import admin
    from sourcer.people import privacy
    from sourcer.people.errors import PeopleError
    from sourcer.tenancy.db import tenant_db

    db = tenant_db("acme")
    old, recent, active = emp(c, "Old"), emp(c, "Recent"), emp(c, "Active")
    for e in (old, recent):
        c.patch(f"/people/employees/{e['id']}", {"status": "inactive"})
    db["employees"].update_one({"_id": ObjectId(old["id"])}, {"$set": {"inactive_at": datetime.now(timezone.utc) - timedelta(days=400)}})
    assert [str(d["_id"]) for d in privacy.due_for_retention(db, 365)] == [old["id"]]
    with pytest.raises(PeopleError):
        privacy.due_for_retention(db, 5)
    admin.main(["people", "retention", "--tenant", "acme", "--days", "365", "--dry-run"])
    assert "would anonymise 1" in capsys.readouterr().out
    assert c.get(f"/people/employees/{old['id']}").json()["name"] == "Old"  # a dry run changes nothing
    admin.main(["people", "retention", "--tenant", "acme", "--days", "365"])
    assert c.get(f"/people/employees/{old['id']}").json()["name"] == "Former employee"
    assert c.get(f"/people/employees/{recent['id']}").json()["name"] == "Recent"
    assert c.get(f"/people/employees/{active['id']}").json()["name"] == "Active"
    assert privacy.run_retention(db, 365) == 0  # nothing left to do
