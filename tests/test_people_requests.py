"""Employees' requests: leave and a change to the last working day, decided by a reporting manager or by HR."""

from datetime import date, timedelta

import pytest

from tests.conftest import KEY

PASSWORD = "a-long-enough-password"
PUB = {"X-Service-Key": KEY, "X-Client-IP": "203.0.113.7"}
# Mondays far enough ahead that nothing here collides with "today"; 2027-03-01 is a Monday.
MON = date(2027, 3, 1)


def day(n):
    return (MON + timedelta(days=n)).isoformat()


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


class Person:
    """An employee with a working login: .e the employee, .h session headers, and small helpers."""

    def __init__(self, api, e, user):
        self.api, self.e = api, e
        self.h = {"X-Service-Key": KEY, "X-Tenant": "acme", "X-User-Id": user["id"], "X-Session-Version": str(user["session_version"])}

    def get(self, path, **kw):
        return self.api.get(path, headers=self.h, **kw)

    def post(self, path, json=None):
        return self.api.post(path, json=json or {}, headers=self.h)


def person(api, c, name, manager=None, joining="2024-01-15"):
    body = {"name": name, "work_email": f"{name.lower()}@acme.com", "joining_date": joining}
    if manager:
        body["manager_id"] = manager.e["id"]
    e = c.post("/people/employees", body).json()
    token = c.post(f"/people/employees/{e['id']}/login-link").json()["token"]
    user = api.post(f"/public/join/{token}", json={"password": PASSWORD}, headers=PUB).json()
    return Person(api, e, user)


def types(c):
    return {t["code"]: t for t in c.get("/people/leave-types").json()}


def ask(p, c, start, end=None, code="PL", **over):
    return p.post("/people/me/leave-requests", {"type_id": types(c)[code]["id"], "start_date": start, "end_date": end, **over})


def used(c, p, year=2027, code="PL"):
    rows = c.get(f"/people/employees/{p.e['id']}/balances", params={"year": year}).json()["types"]
    return next(r for r in rows if r["code"] == code)


@pytest.fixture
def team(api, c):
    """Boss (a manager) with Asha reporting to them, plus Dan who reports to nobody."""
    boss = person(api, c, "Boss")
    asha = person(api, c, "Asha", manager=boss)
    dan = person(api, c, "Dan")
    return boss, asha, dan


# ---------- asking ----------

def test_an_employee_asks_for_leave_and_it_waits(api, c, team):
    boss, asha, _ = team
    pre = asha.post("/people/me/leave-requests/preview", {"type_id": types(c)["PL"]["id"], "start_date": day(0), "end_date": day(4)}).json()
    assert pre["days"] == 5 and pre["years"][0]["remaining_after"] == 13
    res = ask(asha, c, day(0), day(4), reason="Family trip")
    assert res.status_code == 201
    r = res.json()
    assert (r["kind"], r["status"], r["employee_name"], r["reason"]) == ("leave", "pending", "Asha", "Family trip")
    assert r["leave"]["days"] == 5 and r["leave"]["type_name"] == "Planned leave" and r["can_decide"] is False
    assert used(c, asha)["used"] == 0  # nothing is recorded until someone approves
    assert [x["id"] for x in asha.get("/people/me/requests").json()] == [r["id"]]
    assert asha.get("/people/me/leave").json()["total"] == 0


def test_the_page_can_say_who_will_see_it(api, c, team):
    boss, asha, dan = team
    assert asha.get("/people/me/request-routing").json() == {"manager_name": "Boss", "hr": True}
    assert dan.get("/people/me/request-routing").json() == {"manager_name": None, "hr": True}
    c.patch("/people/policy", {"manager_approval": False})
    assert asha.get("/people/me/request-routing").json() == {"manager_name": None, "hr": True}  # only HR now


@pytest.mark.parametrize("bad", [
    {"start_date": "2027-02-30"}, {"start_date": "soon"}, {"start_date": ""}, {"start_date": day(4), "end_date": day(0)},
    {"start_date": day(0), "end_date": "2029-01-01"}, {"start_date": day(0), "day_part": "first_half", "end_date": day(2)},
    {"start_date": day(0), "reason": "x" * 301}, {"start_date": day(5), "end_date": day(6)},  # a weekend: no working days
    {"start_date": "2023-06-05"},  # before they joined
])
def test_bad_requests_are_refused(api, c, team, bad):
    _, asha, _ = team
    body = {"type_id": types(c)["PL"]["id"], **bad}
    assert asha.post("/people/me/leave-requests", body).status_code == 422, bad
    assert asha.get("/people/me/requests").json() == []


def test_more_refusals(api, c, team):
    _, asha, _ = team
    pl = types(c)["PL"]["id"]
    assert asha.post("/people/me/leave-requests", {"type_id": "a" * 24, "start_date": day(0)}).status_code == 404
    assert asha.post("/people/me/leave-requests", {"type_id": pl, "start_date": day(0), "employee_id": team[0].e["id"]}).status_code == 422  # cannot ask for someone else
    old = (date.today() - timedelta(days=45))
    old = old + timedelta(days=(7 - old.weekday()) % 7)
    res = asha.post("/people/me/leave-requests", {"type_id": pl, "start_date": old.isoformat()})
    assert res.status_code == 422 and "recorded by HR" in res.json()["detail"]
    c.patch(f"/people/leave-types/{pl}", {"active": False})
    assert asha.post("/people/me/leave-requests", {"type_id": pl, "start_date": day(0)}).status_code == 422  # a retired type


def test_dates_already_taken_or_already_asked_for_are_refused(api, c, team):
    _, asha, _ = team
    c.post("/people/leave", {"employee_id": asha.e["id"], "type_id": types(c)["SL"]["id"], "start_date": day(1)})
    taken = ask(asha, c, day(0), day(2))
    assert taken.status_code == 422 and day(1) in taken.json()["detail"]
    assert ask(asha, c, day(7), day(8)).status_code == 201
    again = ask(asha, c, day(8), day(9), code="SL")
    assert again.status_code == 422 and "already have a request waiting" in again.json()["detail"]
    assert ask(asha, c, day(14), code="SL").status_code == 201  # other dates are fine
    # two halves of the same day are not a clash
    assert ask(asha, c, day(21), code="PL", day_part="first_half").status_code == 201
    assert ask(asha, c, day(21), code="SL", day_part="second_half").status_code == 201


def test_there_is_a_limit_on_waiting_requests(api, c, team):
    _, asha, _ = team
    dates = [d for d in (day(i) for i in range(0, 60)) if date.fromisoformat(d).weekday() < 5]
    assert all(ask(asha, c, d).status_code == 201 for d in dates[:20])
    res = ask(asha, c, dates[20])
    assert res.status_code == 422 and "too many requests" in res.json()["detail"]


# ---------- HR decides ----------

def test_hr_approves_and_the_leave_is_recorded(api, c, team):
    _, asha, _ = team
    r = ask(asha, c, day(0), day(4), reason="Trip").json()
    listed = c.get("/people/requests").json()
    assert listed["total"] == 1 and listed["items"][0]["employee_name"] == "Asha" and listed["items"][0]["can_decide"] is True
    assert listed["items"][0]["balance"]["years"][0]["remaining_after"] == 13
    done = c.post(f"/people/requests/{r['id']}/approve", {"note": "Enjoy"})
    assert done.status_code == 200
    d = done.json()
    assert d["status"] == "approved" and d["decided_by_kind"] == "hr" and d["decision_note"] == "Enjoy"
    rec = c.get("/people/leave", params={"employee_id": asha.e["id"]}).json()["items"]
    assert len(rec) == 1 and rec[0]["days"] == 5 and rec[0]["approved_by_name"] == "HR" and rec[0]["reason"] == "Trip"
    assert used(c, asha)["used"] == 5
    assert asha.get("/people/me/requests").json()[0]["status"] == "approved"
    assert asha.get("/people/me/leave").json()["total"] == 1
    assert c.post(f"/people/requests/{r['id']}/approve").status_code == 422  # only once
    assert c.post(f"/people/requests/{r['id']}/reject", {"note": "too late"}).status_code == 422
    assert asha.post(f"/people/me/requests/{r['id']}/cancel").status_code == 422  # decided


def test_hr_rejects_with_a_reason_the_employee_can_read(api, c, team):
    _, asha, _ = team
    r = ask(asha, c, day(0)).json()
    assert c.post(f"/people/requests/{r['id']}/reject", {"note": ""}).status_code == 422
    assert c.post(f"/people/requests/{r['id']}/reject", {"note": "ab"}).status_code == 422
    assert c.post(f"/people/requests/{r['id']}/reject", {}).status_code == 422
    done = c.post(f"/people/requests/{r['id']}/reject", {"note": "Release week, please pick another date"}).json()
    assert done["status"] == "rejected"
    mine = asha.get("/people/me/requests").json()[0]
    assert mine["status"] == "rejected" and mine["decision_note"] == "Release week, please pick another date" and mine["decided_by_kind"] == "hr"
    assert used(c, asha)["used"] == 0 and asha.get("/people/me/leave").json()["total"] == 0
    assert c.post(f"/people/requests/{r['id']}/approve").status_code == 422


def test_going_over_the_balance_is_an_hr_decision(api, c, team):
    boss, asha, _ = team
    r = ask(asha, c, day(0), day(25)).json()  # four weeks: 20 days against 18
    seen = c.get(f"/people/requests/{r['id']}").json()
    assert seen["balance"]["over_balance"] is True and seen["balance"]["years"][0]["remaining_after"] == -2
    refused = boss.post(f"/people/requests/{r['id']}/approve")
    assert refused.status_code == 422 and "Only HR can approve it" in refused.json()["detail"]
    assert c.get(f"/people/requests/{r['id']}").json()["status"] == "pending"  # still waiting, and the manager is told why
    assert c.post(f"/people/requests/{r['id']}/approve").status_code == 422  # HR also has to say so on purpose
    ok = c.post(f"/people/requests/{r['id']}/approve", {"allow_over_balance": True})
    assert ok.status_code == 200
    assert c.get("/people/leave", params={"employee_id": asha.e["id"]}).json()["items"][0]["over_balance"] is True


def test_approving_re_checks_the_dates(api, c, team):
    _, asha, _ = team
    r = ask(asha, c, day(0), day(2)).json()
    c.post("/people/leave", {"employee_id": asha.e["id"], "type_id": types(c)["SL"]["id"], "start_date": day(1)})  # HR records something on top, meanwhile
    res = c.post(f"/people/requests/{r['id']}/approve")
    assert res.status_code == 422 and day(1) in res.json()["detail"]
    assert c.get(f"/people/requests/{r['id']}").json()["status"] == "pending"
    assert c.post(f"/people/requests/{r['id']}/reject", {"note": "Already recorded"}).status_code == 200


def test_two_people_approving_at_once_record_the_leave_once(api, c, team):
    from concurrent.futures import ThreadPoolExecutor

    from sourcer.people import requests as reqs
    from sourcer.tenancy.db import tenant_db

    _, asha, _ = team
    r = ask(asha, c, day(0), day(1)).json()
    db = tenant_db("acme")
    hr_user = {"kind": "hr", "user_id": "u", "employee_id": None, "name": "hr"}

    def go(_):
        try:
            return reqs.approve(db, r["id"], hr_user)["status"]
        except Exception as e:  # noqa: BLE001 - the loser gets an error, which is the point
            return type(e).__name__

    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(go, range(6)))
    assert results.count("approved") == 1
    assert db["leave_records"].count_documents({"employee_id": __import__("bson").ObjectId(asha.e["id"])}) == 1


# ---------- the reporting manager decides ----------

def test_a_manager_decides_their_own_teams_requests(api, c, team):
    boss, asha, dan = team
    r = ask(asha, c, day(0), day(2)).json()
    other = ask(dan, c, day(7)).json()
    queue = boss.get("/people/requests").json()
    assert [i["id"] for i in queue["items"]] == [r["id"]]  # Asha's, not Dan's
    assert boss.get("/people/requests/summary").json() == {"pending": 1, "can_approve": True}
    assert c.get("/people/requests/summary").json()["pending"] == 2  # HR sees both
    assert boss.get(f"/people/requests/{other['id']}").status_code == 404
    assert boss.post(f"/people/requests/{other['id']}/approve").status_code == 404
    assert boss.post(f"/people/requests/{other['id']}/reject", {"note": "not yours"}).status_code == 404
    done = boss.post(f"/people/requests/{r['id']}/approve", {"note": "Approved"}).json()
    assert done["status"] == "approved" and done["decided_by_kind"] == "manager" and done["decided_by_name"] == "Boss"
    rec = c.get("/people/leave", params={"employee_id": asha.e["id"]}).json()["items"][0]
    assert rec["approved_by_id"] == boss.e["id"] and rec["approved_by_name"] == "Boss" and rec["days"] == 3
    assert c.get(f"/people/requests/{other['id']}").json()["status"] == "pending"


def test_a_manager_can_reject_too(api, c, team):
    boss, asha, _ = team
    r = ask(asha, c, day(0)).json()
    assert boss.post(f"/people/requests/{r['id']}/reject", {"note": "Please move it a week"}).json()["status"] == "rejected"
    assert asha.get("/people/me/requests").json()[0]["decided_by_name"] == "Boss"


def test_nobody_decides_their_own_request_and_a_nonmanager_decides_nothing(api, c, team):
    boss, asha, dan = team
    mine = ask(boss, c, day(0)).json()  # the manager's own request
    assert boss.get("/people/requests").json()["total"] == 0  # not in their own queue
    assert boss.post(f"/people/requests/{mine['id']}/approve").status_code == 404
    assert c.post(f"/people/requests/{mine['id']}/approve").status_code == 200  # goes to HR
    for path in ("/people/requests", "/people/requests/summary"):
        assert dan.get(path).status_code == 403  # Dan manages nobody
        assert asha.get(path).status_code == 403
    assert dan.post(f"/people/requests/{mine['id']}/approve").status_code == 403


def test_the_company_can_turn_manager_approval_off(api, c, team):
    boss, asha, _ = team
    r = ask(asha, c, day(0)).json()
    assert boss.get("/me").json()["can_approve"] is True
    assert c.patch("/people/policy", {"manager_approval": False}).status_code == 200
    assert boss.get("/me").json()["can_approve"] is False
    assert boss.get("/people/requests").status_code == 403
    assert boss.post(f"/people/requests/{r['id']}/approve").status_code == 403
    assert c.post(f"/people/requests/{r['id']}/approve").status_code == 200  # HR still can
    c.patch("/people/policy", {"manager_approval": True})
    assert boss.get("/me").json()["can_approve"] is True
    assert c.patch("/people/policy", {"manager_approval": "yes"}).status_code == 422
    assert c.other.get("/people/policy").json()["manager_approval"] is True  # another company's own setting


def test_who_can_approve_follows_the_reporting_line(api, c, team):
    boss, asha, dan = team
    assert (boss.get("/me").json()["can_approve"], asha.get("/me").json()["can_approve"], dan.get("/me").json()["can_approve"]) == (True, False, False)
    assert c.get("/me").json()["can_approve"] is True  # HR
    r = ask(asha, c, day(0)).json()
    c.patch(f"/people/employees/{asha.e['id']}", {"manager_id": dan.e["id"]})  # Asha now reports to Dan
    assert boss.get("/people/requests").json()["total"] == 0 and boss.post(f"/people/requests/{r['id']}/approve").status_code == 404  # Boss is still a manager (the role stays until HR removes it), but Asha is not theirs now
    assert [i["id"] for i in dan.get("/people/requests").json()["items"]] == [r["id"]]  # the current manager decides, even for an older request
    assert dan.get("/me").json()["can_approve"] is True and boss.get("/me").json()["can_approve"] is True  # Dan became a manager when given Asha
    assert dan.post(f"/people/requests/{r['id']}/approve").status_code == 200


# ---------- taking it back ----------

def test_an_employee_can_withdraw_a_waiting_request_but_only_their_own(api, c, team):
    boss, asha, dan = team
    r = ask(asha, c, day(0)).json()
    assert dan.post(f"/people/me/requests/{r['id']}/cancel").status_code == 404  # not theirs: it does not exist to them
    assert dan.get("/people/me/requests").json() == []
    done = asha.post(f"/people/me/requests/{r['id']}/cancel").json()
    assert done["status"] == "cancelled" and done["decided_by_kind"] == "employee"
    assert asha.post(f"/people/me/requests/{r['id']}/cancel").status_code == 422
    assert c.post(f"/people/requests/{r['id']}/approve").status_code == 422  # nothing left to decide
    assert ask(asha, c, day(0)).status_code == 201  # the dates are free again
    assert asha.get("/people/me/requests", params={"status": "cancelled"}).json()[0]["id"] == r["id"]
    assert asha.get("/people/me/requests", params={"status": "nope"}).status_code == 422


def test_leaving_withdraws_what_was_waiting(api, c, team):
    _, asha, _ = team
    r = ask(asha, c, day(0)).json()
    c.patch(f"/people/employees/{asha.e['id']}", {"status": "inactive"})
    got = c.get(f"/people/requests/{r['id']}").json()
    assert got["status"] == "cancelled" and got["decided_by_kind"] == "system"
    assert c.post(f"/people/requests/{r['id']}/approve").status_code == 422


# ---------- last working day ----------

def give_notice(c, p, on="2026-10-01"):
    return c.post(f"/people/employees/{p.e['id']}/notice", {"given_on": on})


def test_an_employee_asks_to_move_their_last_working_day(api, c, team):
    boss, asha, dan = team
    assert asha.post("/people/me/notice-requests", {"last_working_day": "2026-11-30", "reason": "Handover"}).status_code == 422  # no notice given
    notice = give_notice(c, asha).json()["notice"]
    assert notice["basis"] == "probation" or notice["basis"] == "notice_period"
    current = notice["last_working_day"]
    for bad in ({"last_working_day": current, "reason": "same"}, {"last_working_day": "2026-09-30", "reason": "before notice"}, {"last_working_day": "x", "reason": "bad date"},
                {"last_working_day": "2027-01-01", "reason": ""}, {"last_working_day": "2027-01-01", "reason": "ab"}):
        assert asha.post("/people/me/notice-requests", bad).status_code == 422, bad
    res = asha.post("/people/me/notice-requests", {"last_working_day": "2027-01-15", "reason": "Asked to finish a handover"})
    assert res.status_code == 201
    r = res.json()
    assert r["kind"] == "notice" and r["notice"] == {"from_date": current, "to_date": "2027-01-15"} and r["status"] == "pending"
    assert asha.post("/people/me/notice-requests", {"last_working_day": "2027-02-01", "reason": "another"}).status_code == 422  # one at a time
    done = boss.post(f"/people/requests/{r['id']}/approve").json()  # the manager may decide this too
    assert done["status"] == "approved"
    after = c.get(f"/people/employees/{asha.e['id']}").json()["notice"]
    assert after["last_working_day"] == "2027-01-15"
    assert after["adjustments"][0]["requested_by"] == "employee" and after["adjustments"][0]["reason"] == "Asked to finish a handover"
    assert asha.get("/people/me").json()["notice"]["last_working_day"] == "2027-01-15"


def test_a_rejected_notice_change_changes_nothing(api, c, team):
    _, asha, _ = team
    notice = give_notice(c, asha).json()["notice"]
    r = asha.post("/people/me/notice-requests", {"last_working_day": "2027-01-15", "reason": "More time please"}).json()
    c.post(f"/people/requests/{r['id']}/reject", {"note": "We need you on the original date"})
    assert c.get(f"/people/employees/{asha.e['id']}").json()["notice"]["last_working_day"] == notice["last_working_day"]
    assert asha.post("/people/me/notice-requests", {"last_working_day": "2027-02-01", "reason": "Trying again"}).status_code == 201  # may ask again


def test_approving_a_notice_change_after_the_notice_was_withdrawn_fails_cleanly(api, c, team):
    _, asha, _ = team
    give_notice(c, asha)
    r = asha.post("/people/me/notice-requests", {"last_working_day": "2027-01-15", "reason": "More time"}).json()
    # HR withdraws the notice before anyone decides
    api.request("DELETE", f"/people/employees/{asha.e['id']}/notice", headers=c.h)
    res = c.post(f"/people/requests/{r['id']}/approve")
    assert res.status_code == 422 and "No notice" in res.json()["detail"]
    assert c.get(f"/people/requests/{r['id']}").json()["status"] == "pending"


# ---------- listing, access, isolation, privacy ----------

def test_the_queue_filters_and_pages(api, c, team):
    boss, asha, dan = team
    a = ask(asha, c, day(0)).json()
    b = ask(asha, c, day(7)).json()
    d = ask(dan, c, day(14)).json()
    c.post(f"/people/requests/{a['id']}/approve")
    c.post(f"/people/requests/{b['id']}/reject", {"note": "Not that week"})
    ids = lambda **q: [i["id"] for i in c.get("/people/requests", params=q).json()["items"]]  # noqa: E731
    assert ids() == [d["id"]]  # pending by default
    assert set(ids(status="decided")) == {a["id"], b["id"]}
    assert ids(status="approved") == [a["id"]] and ids(status="rejected") == [b["id"]]
    assert c.get("/people/requests", params={"status": "decided", "page_size": 1}).json()["total"] == 2
    for bad in ({"status": "nope"}, {"page": 0}, {"page_size": 101}):
        assert c.get("/people/requests", params=bad).status_code == 422, bad
    assert c.get("/people/requests/nope").status_code == 404
    assert boss.get("/people/requests", params={"status": "decided"}).json()["total"] == 2  # a manager's history is their team's


def test_only_those_who_may_decide_can_use_the_queue(api, c, hr, team):
    from sourcer.tenancy import registry

    boss, asha, _ = team
    r = ask(asha, c, day(0)).json()
    rec, first = registry.create_user("rec@acme.com", "acme", "recruiter")
    registry.change_password("rec@acme.com", first, PASSWORD)
    rec = registry.get_user_by_id(rec["_id"])
    for path in ("/people/requests", "/people/requests/summary", f"/people/requests/{r['id']}"):
        assert api.get(path, headers=H("acme", rec)).status_code == 403
    assert api.post(f"/people/requests/{r['id']}/approve", json={}, headers=H("acme", rec)).status_code == 403
    assert api.post("/people/me/leave-requests", json={}, headers=H("acme", rec)).status_code == 403  # not an employee
    assert c.other.get(f"/people/requests/{r['id']}").status_code == 404
    assert c.other.post(f"/people/requests/{r['id']}/approve").status_code == 404
    assert c.other.get("/people/requests").json()["total"] == 0


def test_requests_are_logged_without_the_personal_text(api, c, team):
    from sourcer.tenancy.db import tenant_db

    boss, asha, _ = team
    r = ask(asha, c, day(0), reason="Private medical thing").json()
    boss.post(f"/people/requests/{r['id']}/approve", {"note": "Secret note"})
    log = list(tenant_db("acme")["audit_log"].find())
    assert {"request_created", "request_approved"} <= {a["action"] for a in log}
    assert "Private medical" not in str(log) and "Secret note" not in str(log)


def test_erasing_a_leaver_clears_the_words_in_their_requests(api, c, team):
    from sourcer.people import privacy
    from sourcer.tenancy.db import tenant_db

    boss, asha, dan = team
    r = ask(asha, c, day(0), reason="Private reason").json()
    boss.post(f"/people/requests/{r['id']}/approve", {"note": "Fine by me"})
    q = ask(dan, c, day(7), reason="Dan's reason").json()
    boss_id = boss.e["id"]
    boss.post(f"/people/requests/{q['id']}/approve")  # not Boss's to decide: nothing happens
    c.patch(f"/people/employees/{asha.e['id']}", {"status": "inactive"})
    privacy.anonymise(tenant_db("acme"), asha.e["id"], "test")
    row = tenant_db("acme")["requests"].find_one({"employee_id": __import__("bson").ObjectId(asha.e["id"])})
    assert row["reason"] is None and row["decision_note"] is None and row["leave"]["days"] == 1


def test_audit_and_indexes_exist_for_new_companies(two_tenants):
    from sourcer import admin
    from sourcer.tenancy.db import tenant_db

    admin.main(["indexes", "ensure", "--all"])
    assert any(i["name"].startswith("employee_id_") for i in tenant_db("acme")["requests"].list_indexes())


# ---------- the manager role ----------

def emp_of(c, p):
    return c.get(f"/people/employees/{p.e['id']}").json()


def test_giving_someone_a_team_makes_them_a_manager(api, c):
    boss = person(api, c, "Boss")
    assert emp_of(c, boss)["is_manager"] is False and emp_of(c, boss)["manager_role"] is None
    asha = person(api, c, "Asha", manager=boss)
    e = emp_of(c, boss)
    assert e["is_manager"] is True and e["manager_role"] is True  # switched on when Asha was given to them
    assert boss.get("/me").json()["can_approve"] is True
    assert asha.get("/me").json()["can_approve"] is False
    opts = {o["name"]: o for o in c.get("/people/employees/options").json()}
    assert opts["Boss"]["is_manager"] is True and opts["Asha"]["is_manager"] is False and opts["Asha"]["manager_role"] is None
    listed = {i["name"]: i["is_manager"] for i in c.get("/people/employees").json()["items"]}
    assert listed == {"Boss": True, "Asha": False}


def test_hr_can_make_someone_a_manager_before_they_have_a_team(api, c):
    lead = person(api, c, "Lead")
    assert lead.get("/me").json()["can_approve"] is False
    assert lead.get("/people/requests").status_code == 403
    assert c.patch(f"/people/employees/{lead.e['id']}", {"is_manager": True}).json()["is_manager"] is True
    assert lead.get("/me").json()["can_approve"] is True
    assert lead.get("/people/requests").json()["total"] == 0  # ready, and nothing waiting yet
    assert lead.get("/people/requests/summary").json() == {"pending": 0, "can_approve": True}
    new = person(api, c, "New", manager=lead)
    r = ask(new, c, day(0)).json()
    assert [i["id"] for i in lead.get("/people/requests").json()["items"]] == [r["id"]]


def test_hr_can_take_the_role_away_and_it_stays_off(api, c, team):
    boss, asha, _ = team
    r = ask(asha, c, day(0)).json()
    off = c.patch(f"/people/employees/{boss.e['id']}", {"is_manager": False}).json()
    assert off["is_manager"] is False and off["manager_role"] is False
    assert boss.get("/me").json()["can_approve"] is False
    assert boss.get("/people/requests").status_code == 403 and boss.post(f"/people/requests/{r['id']}/approve").status_code == 403
    assert asha.get("/people/me/request-routing").json() == {"manager_name": None, "hr": True}  # the page says it goes to HR
    person(api, c, "Another", manager=boss)  # giving them more people does not turn it back on
    assert emp_of(c, boss)["is_manager"] is False and boss.get("/me").json()["can_approve"] is False
    assert c.post(f"/people/requests/{r['id']}/approve").status_code == 200  # HR can still decide
    back = c.patch(f"/people/employees/{boss.e['id']}", {"is_manager": None}).json()  # back to following the reporting line
    assert back["manager_role"] is None and back["is_manager"] is True
    assert boss.get("/me").json()["can_approve"] is True


def test_records_from_before_the_role_existed_follow_the_reporting_line(api, c, team):
    from bson import ObjectId

    from sourcer.tenancy.db import tenant_db

    boss, asha, dan = team
    tenant_db("acme")["employees"].update_many({}, {"$unset": {"manager_role": ""}})
    assert boss.get("/me").json()["can_approve"] is True and dan.get("/me").json()["can_approve"] is False
    assert emp_of(c, boss)["is_manager"] is True and emp_of(c, dan)["is_manager"] is False
    r = ask(asha, c, day(0)).json()
    assert boss.post(f"/people/requests/{r['id']}/approve").status_code == 200


def test_only_hr_sets_the_role_and_it_must_be_a_real_yes_or_no(api, c, team):
    boss, asha, _ = team
    assert asha.post(f"/people/employees/{asha.e['id']}", {"is_manager": True}).status_code in (404, 405)
    assert api.patch(f"/people/employees/{asha.e['id']}", json={"is_manager": True}, headers=asha.h).status_code == 403
    assert api.patch(f"/people/employees/{boss.e['id']}", json={"is_manager": False}, headers=boss.h).status_code == 403  # not even their own
    for bad in ("yes", 1, "true", []):
        assert c.patch(f"/people/employees/{asha.e['id']}", {"is_manager": bad}).status_code == 422, bad
    created = c.post("/people/employees", {"name": "Made", "work_email": "made@acme.com", "joining_date": "2024-01-15", "is_manager": True}).json()
    assert created["is_manager"] is True and created["manager_role"] is True


def test_the_company_switch_still_wins_over_the_role(api, c, team):
    boss, asha, _ = team
    c.patch("/people/policy", {"manager_approval": False})
    assert emp_of(c, boss)["is_manager"] is True  # the role stays
    assert boss.get("/me").json()["can_approve"] is False  # but nobody uses it while the company has it off
