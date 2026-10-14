"""Work from home: requests, approvals, the monthly limit, overlap with leave, and who can see what."""

from datetime import date, timedelta

import pytest

from tests.conftest import KEY
from tests.test_people_requests import PUB, PASSWORD, H, Person, c, hr, person  # noqa: F401  (shared fixtures and helpers)

MON = date(2027, 3, 1)  # a Monday; March 2027 has 23 working days


def day(n):
    return (MON + timedelta(days=n)).isoformat()


def types(c):
    return {t["code"]: t for t in c.get("/people/leave-types").json()}


def ask(p, start, end=None, **over):
    return p.post("/people/me/wfh-requests", {"start_date": start, "end_date": end, **over})


@pytest.fixture
def team(api, c):
    boss = person(api, c, "Boss")
    asha = person(api, c, "Asha", manager=boss)
    dan = person(api, c, "Dan")
    return boss, asha, dan


def mine(p, month="2027-03"):
    return p.get("/people/me/wfh", params={"month": month}).json()


# ---------- asking ----------

def test_an_employee_asks_for_work_from_home_and_it_waits(api, c, team):
    _, asha, _ = team
    pre = asha.post("/people/me/wfh-requests/preview", {"start_date": day(0), "end_date": day(4)}).json()
    assert pre["days"] == 5 and pre["over_limit"] is False and pre["conflict"] is None
    assert pre["months"] == [{"month": "2027-03", "label": "March 2027", "days": 5, "used_before": 0, "limit": 0, "over": False}]
    res = ask(asha, day(0), day(4), reason="Internet at the office is down")
    assert res.status_code == 201
    r = res.json()
    assert (r["kind"], r["status"], r["employee_name"]) == ("wfh", "pending", "Asha")
    assert r["wfh"] == {"start_date": day(0), "end_date": day(4), "day_part": "full", "days": 5} and r["leave"] is None
    assert mine(asha)["used"] == 0 and mine(asha)["items"] == []  # nothing is on record until it is approved
    assert [x["id"] for x in asha.get("/people/me/requests").json()] == [r["id"]]


def test_weekends_and_holidays_are_not_counted_and_half_days_work(api, c, team):
    _, asha, _ = team
    c.post("/people/holidays", {"date": day(2), "name": "Holiday"})
    pre = asha.post("/people/me/wfh-requests/preview", {"start_date": day(0), "end_date": day(9)}).json()  # two weeks
    assert pre["days"] == 7 and pre["skipped_weekend"] == 2 and pre["skipped_holidays"] == [{"date": day(2), "name": "Holiday"}]
    half = asha.post("/people/me/wfh-requests/preview", {"start_date": day(7), "day_part": "first_half"}).json()
    assert half["days"] == 0.5
    assert asha.post("/people/me/wfh-requests/preview", {"start_date": day(0), "end_date": day(2), "day_part": "first_half"}).status_code == 422
    assert asha.post("/people/me/wfh-requests/preview", {"start_date": day(5), "end_date": day(6)}).status_code == 422  # a weekend: nothing to count


@pytest.mark.parametrize("bad", [
    {"start_date": "2027-02-30"}, {"start_date": "soon"}, {"start_date": ""}, {"start_date": day(4), "end_date": day(0)},
    {"start_date": day(0), "end_date": "2029-01-01"}, {"start_date": day(0), "reason": "x" * 301}, {"start_date": "2023-06-05"},
])
def test_bad_wfh_requests_are_refused(api, c, team, bad):
    _, asha, _ = team
    assert ask(asha, **{"start": bad.pop("start_date"), **bad}).status_code == 422 if False else asha.post("/people/me/wfh-requests", bad).status_code == 422, bad
    assert asha.get("/people/me/requests").json() == []


def test_an_old_request_is_for_hr_to_record(api, c, team):
    _, asha, _ = team
    old = date.today() - timedelta(days=45)
    old = old + timedelta(days=(7 - old.weekday()) % 7)
    res = ask(asha, old.isoformat())
    assert res.status_code == 422 and "recorded by HR" in res.json()["detail"]
    ok = c.post("/people/wfh", {"employee_id": asha.e["id"], "start_date": old.isoformat()})
    assert ok.status_code == 201  # HR can record what already happened


# ---------- not both leave and WFH ----------

def test_a_day_is_not_both_leave_and_wfh(api, c, team):
    _, asha, _ = team
    leave = c.post("/people/leave", {"employee_id": asha.e["id"], "type_id": types(c)["PL"]["id"], "start_date": day(1)})
    assert leave.status_code == 201
    clash = ask(asha, day(0), day(2))
    assert clash.status_code == 422 and day(1) in clash.json()["detail"]
    assert ask(asha, day(7), day(8)).status_code == 201  # other days are fine
    # and the other way round: leave over a WFH day
    c.post(f"/people/requests/{ask(asha, day(14)).json()['id']}/approve")
    again = c.post("/people/leave", {"employee_id": asha.e["id"], "type_id": types(c)["PL"]["id"], "start_date": day(14)})
    assert again.status_code == 422 and "work from home" in again.json()["detail"]
    over = asha.post("/people/me/leave-requests", {"type_id": types(c)["PL"]["id"], "start_date": day(14)})
    assert over.status_code == 422 and day(14) in over.json()["detail"]


def test_half_a_day_of_each_is_fine(api, c, team):
    _, asha, _ = team
    c.post("/people/leave", {"employee_id": asha.e["id"], "type_id": types(c)["PL"]["id"], "start_date": day(0), "day_part": "first_half"})
    ok = ask(asha, day(0), day_part="second_half")
    assert ok.status_code == 201
    assert c.post(f"/people/requests/{ok.json()['id']}/approve").status_code == 200
    assert ask(asha, day(0), day_part="second_half").status_code == 422  # that half is taken now
    assert ask(asha, day(0)).status_code == 422  # and so is a whole day


def test_two_waiting_requests_cannot_overlap(api, c, team):
    _, asha, _ = team
    assert ask(asha, day(0), day(2)).status_code == 201
    res = ask(asha, day(2), day(3))
    assert res.status_code == 422 and "already have a request waiting" in res.json()["detail"]
    assert ask(asha, day(7), day(8)).status_code == 201


# ---------- deciding ----------

def test_a_manager_approves_and_it_is_tracked(api, c, team):
    boss, asha, dan = team
    r = ask(asha, day(0), day(2), reason="Plumber visiting").json()
    other = ask(dan, day(7)).json()
    assert [i["id"] for i in boss.get("/people/requests").json()["items"]] == [r["id"]]  # Dan's is not Boss's
    queue = boss.get("/people/requests").json()["items"][0]
    assert queue["kind"] == "wfh" and queue["wfh"]["days"] == 3 and queue["wfh_check"]["over_limit"] is False
    done = boss.post(f"/people/requests/{r['id']}/approve", {"note": "Fine"}).json()
    assert done["status"] == "approved" and done["decided_by_kind"] == "manager"
    m = mine(asha)
    assert m["used"] == 3 and len(m["items"]) == 1 and m["items"][0]["approved_by_name"] == "Boss" and m["items"][0]["days"] == 3 and m["items"][0]["reason"] == "Plumber visiting"
    assert mine(dan)["used"] == 0
    assert c.post(f"/people/requests/{other['id']}/approve").status_code == 200  # HR decides anything
    hr_view = c.get("/people/wfh", params={"month": "2027-03"}).json()
    assert hr_view["total"] == 2
    assert {i["approved_by_name"] for i in hr_view["items"]} == {"Boss", "HR"}


def test_a_rejection_needs_a_reason_and_records_nothing(api, c, team):
    _, asha, _ = team
    r = ask(asha, day(0)).json()
    assert c.post(f"/people/requests/{r['id']}/reject", {"note": ""}).status_code == 422
    assert c.post(f"/people/requests/{r['id']}/reject", {"note": "Office day for the demo"}).status_code == 200
    assert mine(asha)["used"] == 0 and asha.get("/people/me/requests").json()[0]["decision_note"] == "Office day for the demo"
    withdrawn = ask(asha, day(7)).json()
    assert asha.post(f"/people/me/requests/{withdrawn['id']}/cancel").json()["status"] == "cancelled"


# ---------- the monthly limit ----------

def test_the_monthly_limit_is_hrs_to_go_past(api, c, team):
    boss, asha, _ = team
    assert c.patch("/people/policy", {"wfh_monthly_limit": 4}).status_code == 200
    r = ask(asha, day(0), day(4)).json()  # 5 days against 4
    seen = c.get(f"/people/requests/{r['id']}").json()
    assert seen["wfh_check"]["over_limit"] is True and seen["wfh_check"]["months"][0]["limit"] == 4
    refused = boss.post(f"/people/requests/{r['id']}/approve")
    assert refused.status_code == 422 and "Only HR can approve it" in refused.json()["detail"]
    assert c.get(f"/people/requests/{r['id']}").json()["status"] == "pending"
    plain = c.post(f"/people/requests/{r['id']}/approve")
    assert plain.status_code == 422 and "record anyway" in plain.json()["detail"]
    assert c.post(f"/people/requests/{r['id']}/approve", {"allow_over_balance": True}).status_code == 200
    rec = mine(asha)
    assert rec["used"] == 5 and rec["limit"] == 4 and rec["items"][0]["over_limit"] is True


def test_the_limit_counts_each_month_on_its_own(api, c, team):
    _, asha, _ = team
    c.patch("/people/policy", {"wfh_monthly_limit": 3})
    for start, end in ((day(0), day(1)),):  # two days in March
        c.post(f"/people/requests/{ask(asha, start, end).json()['id']}/approve")
    pre = asha.post("/people/me/wfh-requests/preview", {"start_date": day(2), "end_date": day(4)}).json()  # three more in March: 5 of 3
    assert pre["over_limit"] is True and pre["months"][0]["used_before"] == 2
    april = (date(2027, 4, 5)).isoformat()
    ok = asha.post("/people/me/wfh-requests/preview", {"start_date": april, "end_date": date(2027, 4, 7).isoformat()}).json()
    assert ok["over_limit"] is False and ok["months"][0]["label"] == "April 2027" and ok["months"][0]["used_before"] == 0
    both = asha.post("/people/me/wfh-requests/preview", {"start_date": date(2027, 3, 30).isoformat(), "end_date": date(2027, 4, 2).isoformat()}).json()
    assert [m["month"] for m in both["months"]] == ["2027-03", "2027-04"]  # a stretch across a month end is counted in each


def test_no_limit_means_no_limit(api, c, team):
    _, asha, _ = team
    assert c.get("/people/policy").json()["wfh_monthly_limit"] == 0
    r = ask(asha, day(0), day(25)).json()
    assert c.post(f"/people/requests/{r['id']}/approve").status_code == 200
    assert mine(asha)["used"] == 20  # every weekday of the stretch, still no limit


def test_policy_settings_are_checked(api, c):
    for bad in ({"wfh_monthly_limit": 32}, {"wfh_monthly_limit": -1}, {"wfh_monthly_limit": "ten"}, {"wfh_enabled": "yes"}, {"wfh_enabled": None}):
        assert c.patch("/people/policy", bad).status_code == 422, bad
    ok = c.patch("/people/policy", {"wfh_monthly_limit": 8, "wfh_enabled": True}).json()
    assert ok["wfh_monthly_limit"] == 8 and ok["wfh_enabled"] is True
    assert c.other.get("/people/policy").json()["wfh_monthly_limit"] == 0  # another company's own setting


def test_a_company_can_switch_it_off(api, c, team):
    boss, asha, _ = team
    r = ask(asha, day(0)).json()
    c.post(f"/people/requests/{r['id']}/approve")
    c.patch("/people/policy", {"wfh_enabled": False})
    for res in (ask(asha, day(7)), asha.post("/people/me/wfh-requests/preview", {"start_date": day(7)}),
                c.post("/people/wfh", {"employee_id": asha.e["id"], "start_date": day(14)}), c.post("/people/wfh/preview", {"employee_id": asha.e["id"], "start_date": day(14)})):
        assert res.status_code == 422 and "not switched on" in res.json()["detail"]
    m = mine(asha)
    assert m["enabled"] is False and m["used"] == 1  # what was recorded is still there to see
    assert c.get("/people/wfh", params={"month": "2027-03"}).json()["total"] == 1


# ---------- HR records and cancels ----------

def test_hr_records_work_from_home_directly_and_can_cancel_it(api, c, team):
    _, asha, _ = team
    res = c.post("/people/wfh", {"employee_id": asha.e["id"], "start_date": day(0), "end_date": day(2), "reason": "Moved house", "note": "Agreed by phone"})
    assert res.status_code == 201
    rec = res.json()
    assert rec["days"] == 3 and rec["approved_by_name"] == "HR" and rec["status"] == "recorded" and rec["employee_name"] == "Asha"
    assert mine(asha)["used"] == 3
    assert c.post("/people/wfh", {"employee_id": asha.e["id"], "start_date": day(1)}).status_code == 422  # taken
    gone = c.post(f"/people/wfh/{rec['id']}/cancel", {"reason": "Came to the office"}).json()
    assert gone["status"] == "cancelled"
    assert mine(asha)["used"] == 0
    assert c.post(f"/people/wfh/{rec['id']}/cancel", {}).status_code == 422
    assert c.post("/people/wfh", {"employee_id": asha.e["id"], "start_date": day(1)}).status_code == 201  # the days are free again


def test_hr_recording_checks_the_same_things(api, c, team):
    _, asha, _ = team
    for bad in ({"start_date": "2023-06-05"}, {"start_date": "x"}, {"start_date": day(5), "end_date": day(6)}, {"start_date": day(0), "end_date": day(2), "day_part": "first_half"},
                {"start_date": day(0), "approved_by_id": "z" * 24}, {"start_date": day(0), "surprise": 1}):
        assert c.post("/people/wfh", {"employee_id": asha.e["id"], **bad}).status_code in (404, 422), bad
    assert c.post("/people/wfh", {"employee_id": "nope", "start_date": day(0)}).status_code == 404
    c.patch(f"/people/employees/{asha.e['id']}", {"status": "inactive"})
    assert c.post("/people/wfh", {"employee_id": asha.e["id"], "start_date": day(0)}).status_code == 422
    c.patch("/people/policy", {"wfh_monthly_limit": 2})
    dan = person(api, c, "Eve")
    res = c.post("/people/wfh", {"employee_id": dan.e["id"], "start_date": day(0), "end_date": day(3)})
    assert res.status_code == 422 and "record anyway" in res.json()["detail"]
    assert c.post("/people/wfh", {"employee_id": dan.e["id"], "start_date": day(0), "end_date": day(3), "allow_over_limit": True}).json()["over_limit"] is True


def test_only_hr_records_or_cancels(api, c, team):
    boss, asha, _ = team
    rec = c.post("/people/wfh", {"employee_id": asha.e["id"], "start_date": day(0)}).json()
    for who in (boss, asha):
        assert who.post("/people/wfh", {"employee_id": asha.e["id"], "start_date": day(7)}).status_code == 403
        assert who.post("/people/wfh/preview", {"employee_id": asha.e["id"], "start_date": day(7)}).status_code == 403
        assert who.post(f"/people/wfh/{rec['id']}/cancel", {}).status_code == 403


# ---------- who can see what ----------

def test_the_month_view_for_hr_and_for_a_manager(api, c, team):
    boss, asha, dan = team
    c.post("/people/wfh", {"employee_id": asha.e["id"], "start_date": day(0), "end_date": day(2)})
    c.post("/people/wfh", {"employee_id": asha.e["id"], "start_date": day(7), "day_part": "first_half"})
    c.post("/people/wfh", {"employee_id": dan.e["id"], "start_date": day(1)})
    everyone = c.get("/people/wfh/summary", params={"month": "2027-03"}).json()
    assert everyone["label"] == "March 2027" and everyone["total_days"] == 4.5
    assert [(p["name"], p["days"]) for p in everyone["people"]] == [("Asha", 3.5), ("Dan", 1)]
    assert [d["date"] for d in everyone["days"]] == [day(0), day(1), day(2), day(7)]
    on_tuesday = next(d for d in everyone["days"] if d["date"] == day(1))
    assert [p["name"] for p in on_tuesday["people"]] == ["Asha", "Dan"]
    assert next(d for d in everyone["days"] if d["date"] == day(7))["people"][0]["part"] == "first_half"
    team_view = boss.get("/people/wfh/summary", params={"month": "2027-03"}).json()
    assert [p["name"] for p in team_view["people"]] == ["Asha"] and team_view["total_days"] == 3.5  # Dan is not on Boss's team
    assert boss.get("/people/wfh", params={"month": "2027-03"}).json()["total"] == 2
    assert boss.get("/people/wfh", params={"employee_id": dan.e["id"]}).json()["total"] == 0  # not theirs to look at
    assert boss.get("/people/wfh", params={"employee_id": asha.e["id"]}).json()["total"] == 2
    assert c.get("/people/wfh/summary", params={"month": "2027-04"}).json()["people"] == []
    assert asha.get("/people/wfh/summary").status_code == 403 and dan.get("/people/wfh").status_code == 403  # not managers


def test_month_and_filter_inputs_are_checked(api, c, team):
    for bad in ({"month": "2027-13"}, {"month": "March"}, {"month": "2027-3"}, {"month": "27-03"}):
        assert c.get("/people/wfh/summary", params=bad).status_code == 422, bad
        assert c.get("/people/wfh", params=bad).status_code == 422, bad
    assert c.get("/people/wfh", params={"status": "nope"}).status_code == 422
    assert c.get("/people/wfh", params={"page": 0}).status_code == 422
    assert c.get("/people/wfh", params={"page_size": 101}).status_code == 422
    assert c.get("/people/wfh/summary").json()["month"] == date.today().strftime("%Y-%m")  # this month by default


def test_an_employee_sees_only_their_own_and_another_company_sees_none(api, c, team):
    boss, asha, dan = team
    c.post("/people/wfh", {"employee_id": asha.e["id"], "start_date": day(0)})
    c.post("/people/wfh", {"employee_id": dan.e["id"], "start_date": day(1)})
    assert [i["employee_name"] for i in mine(dan)["items"]] == ["Dan"] and mine(asha)["used"] == 1
    assert c.other.get("/people/wfh", params={"month": "2027-03"}).json()["total"] == 0
    rec_id = c.get("/people/wfh", params={"month": "2027-03"}).json()["items"][0]["id"]
    assert c.other.post(f"/people/wfh/{rec_id}/cancel", {}).status_code == 404
    assert c.other.post("/people/wfh", {"employee_id": asha.e["id"], "start_date": day(7)}).status_code == 404


def test_leaving_withdraws_a_waiting_request_and_erasing_clears_the_words(api, c, team):
    from bson import ObjectId

    from sourcer.people import privacy
    from sourcer.tenancy.db import tenant_db

    _, asha, _ = team
    waiting = ask(asha, day(0), reason="Private reason").json()
    done = c.post("/people/wfh", {"employee_id": asha.e["id"], "start_date": day(7), "reason": "Medical", "note": "Secret note"}).json()
    c.patch(f"/people/employees/{asha.e['id']}", {"status": "inactive"})
    assert c.get(f"/people/requests/{waiting['id']}").json()["status"] == "cancelled"
    privacy.anonymise(tenant_db("acme"), asha.e["id"], "test")
    row = tenant_db("acme")["wfh_records"].find_one({"_id": ObjectId(done["id"])})
    assert row["reason"] is None and row["note"] is None and row["days"] == 1  # the numbers stay


def test_it_is_logged_without_the_personal_text(api, c, team):
    from sourcer.tenancy.db import tenant_db

    boss, asha, _ = team
    r = ask(asha, day(0), reason="Private medical thing").json()
    boss.post(f"/people/requests/{r['id']}/approve", {"note": "Secret"})
    c.post("/people/wfh", {"employee_id": asha.e["id"], "start_date": day(7), "reason": "Another private one"})
    log = list(tenant_db("acme")["audit_log"].find())
    assert {"request_created", "request_approved", "wfh_recorded"} <= {a["action"] for a in log}
    assert "Private medical" not in str(log) and "Another private" not in str(log) and "Secret" not in str(log)


def test_wfh_indexes_exist(two_tenants):
    from sourcer import admin
    from sourcer.tenancy.db import tenant_db

    admin.main(["indexes", "ensure", "--all"])
    assert any(i["name"].startswith("employee_id_") for i in tenant_db("acme")["wfh_records"].list_indexes())
