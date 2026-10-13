"""Company policy, leave types, holidays, leave records, balances, probation and notice.

Calendar facts used: 2026-03-02 is a Monday (so 03-06 Fri, 03-07 Sat, 03-08 Sun, 03-09 Mon, 03-10 Tue);
2025-03-03 is a Monday. Employees join on 2024-01-15 unless a test says otherwise.
"""

import pytest

from tests.conftest import KEY


def H(tenant, user):
    return {"X-Service-Key": KEY, "X-Tenant": tenant, "X-User-Id": str(user["_id"])}


@pytest.fixture
def hr(two_tenants):
    from sourcer.tenancy import registry

    return {slug: registry.get_user_by_id(v["user_id"]) for slug, v in two_tenants.items()}


@pytest.fixture
def c(api, hr):
    """A tiny client bound to acme's HR user (and one for globex): c.get/post/patch/delete(path, ...)."""

    class Client:
        def __init__(self, tenant):
            self.tenant, self.h = tenant, H(tenant, hr[tenant])

        def get(self, path, **kw):
            return api.get(path, headers=self.h, **kw)

        def post(self, path, json=None, **kw):
            return api.post(path, json=json or {}, headers=self.h, **kw)

        def patch(self, path, json=None):
            return api.patch(path, json=json or {}, headers=self.h)

        def delete(self, path):
            return api.delete(path, headers=self.h)

    out = Client("acme")
    out.other = Client("globex")
    return out


def emp(c, name="Asha", joining="2024-01-15", **over):
    res = c.post("/people/employees", {"name": name, "work_email": f"{name.lower().replace(' ', '')}@{c.tenant}.com", "joining_date": joining, **over})
    assert res.status_code == 201, res.text
    return res.json()


def types(c):
    return {t["code"]: t for t in c.get("/people/leave-types").json()}


def leave(c, e, code, start, end=None, **over):
    return c.post("/people/leave", {"employee_id": e["id"], "type_id": types(c)[code]["id"], "start_date": start, "end_date": end, **over})


def bal(c, e, year, code="PL"):
    rows = c.get(f"/people/employees/{e['id']}/balances", params={"year": year}).json()["types"]
    return next(r for r in rows if r["code"] == code)


# ---------- company policy ----------

def test_defaults_for_a_new_company(c):
    p = c.get("/people/policy").json()
    assert p["probation_months"] == 6 and p["notice_period"] == {"amount": 2, "unit": "months"}
    assert p["probation_notice_period"] == {"amount": 7, "unit": "days"}
    assert p["weekend_days"] == [5, 6] and p["year_start_month"] == 1 and p["prorate_new_joiners"] is False
    t = types(c)
    assert set(t) == {"PL", "SL", "UL"}  # no casual leave
    assert (t["PL"]["days_per_year"], t["SL"]["days_per_year"], t["UL"]["days_per_year"]) == (18, 12, None)
    assert t["UL"]["paid"] is False and t["PL"]["paid"] is True


def test_starting_types_are_seeded_once_and_hr_can_remove_them(c):
    for t in types(c).values():
        assert c.patch(f"/people/leave-types/{t['id']}", {"active": False}).status_code == 200
    assert c.get("/people/leave-types").json() == []  # none in use, and the defaults do not come back
    assert len(c.get("/people/leave-types", params={"include_inactive": True}).json()) == 3


def test_each_company_has_its_own_rules(c):
    assert c.patch("/people/policy", {"probation_months": 3, "notice_period": {"amount": 1, "unit": "months"}, "weekend_days": [4, 5]}).status_code == 200
    c.post("/people/leave-types", {"code": "cl", "name": "Casual leave", "days_per_year": 6})
    p = c.other.get("/people/policy").json()
    assert p["probation_months"] == 6 and p["notice_period"]["amount"] == 2 and p["weekend_days"] == [5, 6]
    assert "CL" not in types(c.other) and "CL" in types(c)
    assert emp(c.other)["probation"]["end_date"] == "2024-07-15"  # six months, the other company's rule
    assert emp(c)["probation"]["end_date"] == "2024-04-15"  # three months here


@pytest.mark.parametrize("bad", [
    {"year_start_month": 0}, {"year_start_month": 13}, {"weekend_days": [1, 2, 3, 4]}, {"weekend_days": [5, 5]}, {"weekend_days": [7]},
    {"weekend_days": [-1]}, {"probation_months": 25}, {"probation_months": -1}, {"notice_period": {"amount": 2, "unit": "weeks"}},
    {"notice_period": {"amount": -1, "unit": "days"}}, {"notice_period": {"amount": 25, "unit": "months"}}, {"notice_period": {"amount": 2}},
    {"prorate_new_joiners": "yes"}, {"surprise": 1}, {"probation_months": None},
])
def test_policy_rejects_bad_values(c, bad):
    before = c.get("/people/policy").json()
    assert c.patch("/people/policy", bad).status_code == 422, bad
    assert c.get("/people/policy").json() == before


def test_policy_change_is_audited_by_name_only(c):
    from sourcer.tenancy.db import tenant_db

    c.patch("/people/policy", {"probation_months": 4})
    log = list(tenant_db("acme")["audit_log"].find({"action": "policy_updated"}))
    assert log[0]["detail"] == {"fields": ["probation_months"]}


# ---------- leave types ----------

def test_leave_type_create_edit_and_rules(c):
    res = c.post("/people/leave-types", {"code": "mat", "name": "Maternity", "days_per_year": 90, "counts_weekends": True, "carry_forward_max": 0})
    assert res.status_code == 201 and res.json()["code"] == "MAT" and res.json()["counts_weekends"] is True
    t = res.json()
    assert c.post("/people/leave-types", {"code": "MAT", "name": "Again", "days_per_year": 1}).status_code == 422  # same code
    edited = c.patch(f"/people/leave-types/{t['id']}", {"days_per_year": 84.5, "name": "Maternity leave"}).json()
    assert edited["days_per_year"] == 84.5 and edited["code"] == "MAT"
    assert c.patch(f"/people/leave-types/{t['id']}", {"code": "XX"}).status_code == 422  # codes never change
    assert c.patch(f"/people/leave-types/{t['id']}", {"code": "MAT"}).status_code == 200
    for bad in ({"code": "a"}, {"code": "has space"}, {"code": "TOOLONGCODE"}, {"name": ""}, {"days_per_year": 1.3}, {"days_per_year": -1}, {"days_per_year": 400}, {"carry_forward_max": 0.3}):
        assert c.post("/people/leave-types", {"code": "ZZ", "name": "Z", "days_per_year": 5, **bad}).status_code == 422, bad
    assert c.patch("/people/leave-types/nope", {"name": "x"}).status_code == 404


def test_a_retired_type_cannot_be_used_but_keeps_history(c):
    e = emp(c)
    assert leave(c, e, "SL", "2026-03-02").status_code == 201
    c.patch(f"/people/leave-types/{types(c)['SL']['id']}", {"active": False})
    assert c.post("/people/leave", {"employee_id": e["id"], "type_id": types(c).get("SL", {"id": "0" * 24})["id"], "start_date": "2026-03-03"}).status_code in (404, 422)
    sick = c.get("/people/leave-types", params={"include_inactive": True}).json()
    sid = next(t["id"] for t in sick if t["code"] == "SL")
    res = c.post("/people/leave", {"employee_id": e["id"], "type_id": sid, "start_date": "2026-03-03"})
    assert res.status_code == 422 and "no longer in use" in res.json()["detail"]
    row = bal(c, e, 2026, "SL")
    assert row["used"] == 1 and row["active"] is False  # still shown, because it has history


# ---------- holidays ----------

def test_holidays(c):
    h = c.post("/people/holidays", {"date": "2026-03-09", "name": "Founders day"})
    assert h.status_code == 201
    assert c.post("/people/holidays", {"date": "2026-03-09", "name": "Again"}).status_code == 422
    c.post("/people/holidays", {"date": "2027-01-26", "name": "Republic day"})
    assert [x["name"] for x in c.get("/people/holidays", params={"year": 2026}).json()] == ["Founders day"]
    assert len(c.get("/people/holidays").json()) == 2
    moved = c.patch(f"/people/holidays/{h.json()['id']}", {"date": "2026-03-10"}).json()
    assert moved["date"] == "2026-03-10"
    assert c.get("/people/holidays", params={"year": 2026}).json()[0]["date"] == "2026-03-10"
    assert c.other.get("/people/holidays").json() == []  # not shared with the other company
    for bad in ({"date": "2026-02-30", "name": "x"}, {"date": "tomorrow", "name": "x"}, {"date": "2026-01-01", "name": ""}, {"date": "2026-01-01", "name": "x" * 81}, {"date": "1960-01-01", "name": "x"}):
        assert c.post("/people/holidays", bad).status_code == 422, bad
    assert c.delete(f"/people/holidays/{h.json()['id']}").status_code == 204
    assert c.delete(f"/people/holidays/{h.json()['id']}").status_code == 404
    assert c.other.delete(f"/people/holidays/{h.json()['id']}").status_code == 404


# ---------- counting days ----------

def test_working_days_skip_weekends_and_holidays(c):
    e = emp(c)
    assert leave(c, e, "PL", "2026-03-02", "2026-03-06").json()["days"] == 5  # Mon-Fri
    ben = emp(c, name="Ben")
    assert leave(c, ben, "SL", "2026-03-06", "2026-03-10").json()["days"] == 3  # Fri, Mon, Tue: the weekend is skipped
    c.post("/people/holidays", {"date": "2026-03-17", "name": "Holiday"})
    pre = c.post("/people/leave/preview", {"employee_id": e["id"], "type_id": types(c)["PL"]["id"], "start_date": "2026-03-16", "end_date": "2026-03-18"}).json()
    assert pre["days"] == 2 and pre["skipped_holidays"] == [{"date": "2026-03-17", "name": "Holiday"}]


def test_half_days(c):
    e = emp(c)
    r = leave(c, e, "PL", "2026-03-02", day_part="first_half")
    assert r.status_code == 201 and r.json()["days"] == 0.5 and r.json()["day_part"] == "first_half"
    assert leave(c, e, "PL", "2026-03-03", "2026-03-04", day_part="second_half").status_code == 422  # a half day is one date
    assert leave(c, e, "PL", "2026-03-03", day_part="whole").status_code == 422


def test_nothing_to_record_on_a_weekend(c):
    e = emp(c)
    res = leave(c, e, "PL", "2026-03-07", "2026-03-08")
    assert res.status_code == 422 and "no working days" in res.json()["detail"]


def test_types_that_count_weekends_count_every_day(c):
    e = emp(c)
    t = c.post("/people/leave-types", {"code": "MAT", "name": "Maternity", "days_per_year": 90, "counts_weekends": True}).json()
    c.post("/people/holidays", {"date": "2026-03-09", "name": "H"})
    res = c.post("/people/leave", {"employee_id": e["id"], "type_id": t["id"], "start_date": "2026-03-06", "end_date": "2026-03-10"})
    assert res.json()["days"] == 5  # Fri Sat Sun Mon(holiday) Tue


def test_the_companys_own_weekend_is_used(c):
    e = emp(c)
    c.patch("/people/policy", {"weekend_days": [4, 5]})  # Friday and Saturday off
    assert leave(c, e, "PL", "2026-03-02", "2026-03-08").json()["days"] == 5  # Mon-Thu + Sun
    assert c.other.get("/people/policy").json()["weekend_days"] == [5, 6]


@pytest.mark.parametrize("bad", [
    {"start_date": "2026-03-10", "end_date": "2026-03-02"}, {"start_date": "2023-12-29"}, {"start_date": "2026-02-30"}, {"start_date": "soon"},
    {"start_date": "2026-03-02", "end_date": "2027-03-10"}, {"start_date": "2040-01-01"}, {"start_date": ""},
    {"start_date": "2026-03-02", "reason": "x" * 301}, {"start_date": "2026-03-02", "note": "x" * 501},
])
def test_leave_rejects_bad_input(c, bad):
    e = emp(c)
    body = {"employee_id": e["id"], "type_id": types(c)["PL"]["id"], **bad}
    assert c.post("/people/leave", body).status_code == 422, bad
    assert c.get("/people/leave").json()["total"] == 0


def test_leave_needs_a_real_active_employee_and_type(c):
    e = emp(c)
    pl = types(c)["PL"]["id"]
    assert c.post("/people/leave", {"employee_id": "a" * 24, "type_id": pl, "start_date": "2026-03-02"}).status_code == 404
    assert c.post("/people/leave", {"employee_id": "nope", "type_id": pl, "start_date": "2026-03-02"}).status_code == 404
    assert c.post("/people/leave", {"employee_id": e["id"], "type_id": "b" * 24, "start_date": "2026-03-02"}).status_code == 404
    assert c.post("/people/leave", {"employee_id": e["id"], "type_id": pl, "start_date": "2026-03-02", "extra": 1}).status_code == 422
    c.patch(f"/people/employees/{e['id']}", {"status": "inactive"})
    res = c.post("/people/leave", {"employee_id": e["id"], "type_id": pl, "start_date": "2026-03-02"})
    assert res.status_code == 422 and "active employees" in res.json()["detail"]


# ---------- clashes ----------

def test_no_two_leaves_on_the_same_day_unless_they_are_two_halves(c):
    e = emp(c)
    assert leave(c, e, "PL", "2026-03-02", "2026-03-04").status_code == 201
    assert leave(c, e, "SL", "2026-03-04", "2026-03-05").status_code == 422  # 4 March is taken
    assert leave(c, e, "SL", "2026-03-05").status_code == 201  # the day after is fine
    assert leave(c, e, "PL", "2026-03-09", day_part="first_half").status_code == 201
    assert leave(c, e, "SL", "2026-03-09", day_part="second_half").status_code == 201  # the other half
    clash = leave(c, e, "SL", "2026-03-09", day_part="second_half")
    assert clash.status_code == 422 and "2026-03-09" in clash.json()["detail"]
    assert leave(c, e, "SL", "2026-03-09").status_code == 422  # a full day over two halves
    other_person = emp(c, name="Ben")
    assert leave(c, other_person, "PL", "2026-03-02", "2026-03-04").status_code == 201  # someone else, same dates: fine


def test_a_cancelled_leave_frees_its_dates_and_its_days(c):
    e = emp(c)
    r = leave(c, e, "PL", "2026-03-02", "2026-03-04").json()
    assert bal(c, e, 2026)["remaining"] == 15
    cancelled = c.post(f"/people/leave/{r['id']}/cancel", {"reason": "plans changed"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert bal(c, e, 2026)["remaining"] == 18
    assert leave(c, e, "PL", "2026-03-02", "2026-03-04").status_code == 201  # same dates again
    assert c.post(f"/people/leave/{r['id']}/cancel", {}).status_code == 422  # already cancelled


# ---------- balances ----------

def test_balances_by_type(c):
    e = emp(c)
    leave(c, e, "PL", "2026-03-02", "2026-03-06")
    leave(c, e, "SL", "2026-04-01")
    leave(c, e, "UL", "2026-05-04", "2026-05-06")
    pl, sl, ul = bal(c, e, 2026, "PL"), bal(c, e, 2026, "SL"), bal(c, e, 2026, "UL")
    assert (pl["entitlement"], pl["used"], pl["remaining"]) == (18, 5, 13)
    assert (sl["entitlement"], sl["used"], sl["remaining"]) == (12, 1, 11)
    assert (ul["entitlement"], ul["used"], ul["remaining"]) == (None, 3, None)  # no limit
    other = emp(c, name="Ben")
    assert bal(c, other, 2026)["used"] == 0  # a person's balance is theirs alone


def test_going_over_needs_a_deliberate_yes(c):
    e = emp(c)
    leave(c, e, "PL", "2026-01-05", "2026-01-30")  # 20 working days: too many for 18
    assert c.get("/people/leave").json()["total"] == 0
    res = leave(c, e, "PL", "2026-01-05", "2026-01-30")
    assert res.status_code == 422 and "only 18 left" in res.json()["detail"] and "record anyway" in res.json()["detail"]
    pre = c.post("/people/leave/preview", {"employee_id": e["id"], "type_id": types(c)["PL"]["id"], "start_date": "2026-01-05", "end_date": "2026-01-30"}).json()
    assert pre["over_balance"] is True and pre["years"][0]["remaining_after"] == -2
    ok = leave(c, e, "PL", "2026-01-05", "2026-01-30", allow_over_balance=True)
    assert ok.status_code == 201 and ok.json()["over_balance"] is True
    assert bal(c, e, 2026)["remaining"] == -2
    assert leave(c, e, "UL", "2026-02-02", "2026-02-27").status_code == 201  # unpaid has no limit


def test_adjustments_change_the_balance_and_are_checked(c):
    e = emp(c)
    pl = types(c)["PL"]["id"]
    ok = c.post(f"/people/employees/{e['id']}/leave-adjustments", {"type_id": pl, "year": 2026, "days": 2, "reason": "Granted for weekend work"})
    assert ok.status_code == 201
    assert bal(c, e, 2026)["remaining"] == 20 and bal(c, e, 2026)["adjusted"] == 2
    c.post(f"/people/employees/{e['id']}/leave-adjustments", {"type_id": pl, "year": 2026, "days": -1.5, "reason": "Correction"})
    assert bal(c, e, 2026)["remaining"] == 18.5
    assert [a["reason"] for a in c.get(f"/people/employees/{e['id']}/leave-adjustments").json()] == ["Correction", "Granted for weekend work"]
    for bad in ({"days": 0}, {"days": 0.3}, {"days": 200}, {"reason": ""}, {"year": 2020}, {"year": 2090}, {"type_id": types(c)["UL"]["id"]}, {"type_id": "c" * 24}):
        body = {"type_id": pl, "year": 2026, "days": 1, "reason": "x", **bad}
        assert c.post(f"/people/employees/{e['id']}/leave-adjustments", body).status_code in (404, 422), bad
    assert bal(c, e, 2026)["remaining"] == 18.5


def test_carry_forward_is_capped_per_type(c):
    e = emp(c, joining="2025-01-10")
    cf = c.post("/people/leave-types", {"code": "CF", "name": "Carry", "days_per_year": 10, "carry_forward_max": 4}).json()
    c.post("/people/leave", {"employee_id": e["id"], "type_id": cf["id"], "start_date": "2025-03-03", "end_date": "2025-03-10"})  # 6 days
    assert bal(c, e, 2025, "CF")["remaining"] == 4
    row = bal(c, e, 2026, "CF")
    assert (row["carried_in"], row["entitlement"], row["remaining"]) == (4, 10, 14)
    assert bal(c, e, 2026, "PL")["carried_in"] == 0  # planned leave carries nothing by default
    c.patch(f"/people/leave-types/{cf['id']}", {"carry_forward_max": 2})
    assert bal(c, e, 2026, "CF")["carried_in"] == 2


def test_a_leave_across_new_year_counts_in_each_year(c):
    e = emp(c)
    r = leave(c, e, "PL", "2025-12-29", "2026-01-02").json()  # Mon 29 - Fri 2 Jan: 5 working days
    assert r["days"] == 5
    assert bal(c, e, 2025)["used"] == 3 and bal(c, e, 2026)["used"] == 2


def test_the_leave_year_can_start_in_april(c):
    e = emp(c)
    c.patch("/people/policy", {"year_start_month": 4})
    leave(c, e, "PL", "2026-03-02")  # March belongs to the year that began in April 2025
    leave(c, e, "PL", "2026-04-01")
    b25 = c.get(f"/people/employees/{e['id']}/balances", params={"year": 2025}).json()
    assert b25["label"] == "2025-26" and (b25["starts"], b25["ends"]) == ("2025-04-01", "2026-03-31")
    assert next(r for r in b25["types"] if r["code"] == "PL")["used"] == 1
    assert bal(c, e, 2026)["used"] == 1


def test_new_joiners_get_the_full_or_a_prorated_entitlement(c):
    e = emp(c, joining="2026-07-15")
    assert bal(c, e, 2026)["entitlement"] == 18  # default: full
    c.patch("/people/policy", {"prorate_new_joiners": True})
    assert bal(c, e, 2026)["entitlement"] == 9  # July to December = 6 of 12 months
    assert bal(c, emp(c, name="Old", joining="2024-02-01"), 2026)["entitlement"] == 18  # joined before the year: full
    assert bal(c, emp(c, name="Jan", joining="2026-01-20"), 2026)["entitlement"] == 18
    assert bal(c, emp(c, name="Dec", joining="2026-12-01"), 2026)["entitlement"] == 1.5  # one month, to the half day


def test_balances_for_years_before_joining_or_far_ahead(c):
    e = emp(c, joining="2026-07-15")
    assert c.get(f"/people/employees/{e['id']}/balances", params={"year": 2020}).json()["year"] == 2026  # starts at joining
    assert c.get(f"/people/employees/{e['id']}/balances", params={"year": 1900}).status_code == 422
    assert c.get("/people/employees/nope/balances").status_code == 404


# ---------- preview matches create ----------

def test_preview_shows_what_would_happen_and_saves_nothing(c):
    e = emp(c)
    leave(c, e, "PL", "2026-03-02")
    pre = c.post("/people/leave/preview", {"employee_id": e["id"], "type_id": types(c)["PL"]["id"], "start_date": "2026-03-02", "end_date": "2026-03-04"}).json()
    assert pre["conflict"] and "2026-03-02" in pre["conflict"]
    pre = c.post("/people/leave/preview", {"employee_id": e["id"], "type_id": types(c)["PL"]["id"], "start_date": "2026-03-03", "end_date": "2026-03-04"}).json()
    assert pre["conflict"] is None and pre["days"] == 2
    assert pre["years"] == [{"label": "2026", "days": 2, "remaining_before": 17, "remaining_after": 15}]
    assert c.get("/people/leave").json()["total"] == 1  # preview saved nothing
    assert c.post("/people/leave/preview", {"employee_id": e["id"], "type_id": types(c)["PL"]["id"], "start_date": "x"}).status_code == 422


# ---------- records: edit, list, approver ----------

def test_edit_details_but_not_days(c):
    e = emp(c)
    boss = emp(c, name="Boss")
    r = leave(c, e, "PL", "2026-03-02", reason="Trip").json()
    done = c.patch(f"/people/leave/{r['id']}", {"note": "Booked", "approved_by_id": boss["id"]}).json()
    assert done["note"] == "Booked" and done["approved_by_name"] == "Boss" and done["reason"] == "Trip"
    assert c.patch(f"/people/leave/{r['id']}", {"approved_by_id": None}).json()["approved_by_name"] is None
    for bad in ({"start_date": "2026-03-03"}, {"days": 9}, {"status": "cancelled"}, {"employee_id": boss["id"]}):
        assert c.patch(f"/people/leave/{r['id']}", bad).status_code == 422, bad
    assert c.patch(f"/people/leave/{r['id']}", {"approved_by_id": "d" * 24}).status_code == 404
    assert c.patch(f"/people/leave/{r['id']}", {"reason": "x" * 301}).status_code == 422
    assert c.get(f"/people/leave/{r['id']}").json()["days"] == 1


def test_approver_is_optional_and_must_exist(c):
    e = emp(c)
    assert leave(c, e, "PL", "2026-03-02").json()["approved_by_id"] is None
    assert leave(c, e, "PL", "2026-03-03", approved_by_id="e" * 24).status_code == 404


def test_listing_filters_and_paging(c):
    a, b = emp(c), emp(c, name="Ben")
    leave(c, a, "PL", "2026-03-02")
    leave(c, a, "SL", "2026-03-03")
    leave(c, b, "PL", "2026-04-06")
    gone = leave(c, b, "PL", "2026-05-04").json()
    c.post(f"/people/leave/{gone['id']}/cancel", {})
    ids = lambda **q: [r["start_date"] for r in c.get("/people/leave", params=q).json()["items"]]  # noqa: E731
    assert ids() == ["2026-05-04", "2026-04-06", "2026-03-03", "2026-03-02"]  # newest first
    assert ids(employee_id=a["id"]) == ["2026-03-03", "2026-03-02"]
    assert ids(type_id=types(c)["SL"]["id"]) == ["2026-03-03"]
    assert ids(status="cancelled") == ["2026-05-04"] and len(ids(status="recorded")) == 3
    assert ids(date_from="2026-04-01", date_to="2026-04-30") == ["2026-04-06"]
    assert c.get("/people/leave", params={"employee_id": a["id"]}).json()["items"][0]["employee_name"] == "Asha"
    page = c.get("/people/leave", params={"page_size": 2, "page": 2}).json()
    assert page["total"] == 4 and len(page["items"]) == 2
    for bad in ({"status": "x"}, {"page": 0}, {"page_size": 101}, {"date_from": "nope"}, {"employee_id": "x" * 25}):
        assert c.get("/people/leave", params=bad).status_code == 422, bad
    assert c.get("/people/leave", params={"employee_id": "nope"}).status_code == 404


# ---------- probation ----------

def test_a_new_employee_starts_on_probation_for_the_companys_months(c):
    e = emp(c, joining="2025-08-31")
    assert e["probation"]["end_date"] == "2026-02-28"  # six months, clamped to the month's end
    assert e["probation"]["confirmed_on"] is None and e["probation"]["extensions"] == []
    assert emp(c, name="Fresh", joining="2026-10-01")["probation"]["status"] == "on_probation"
    c.patch("/people/policy", {"probation_months": 0})
    none = emp(c, name="Direct", joining="2026-01-05")
    assert none["probation"]["status"] == "confirmed" and none["probation"]["confirmed_on"] == "2026-01-05"


def test_ended_probation_waits_for_hr_to_decide(c):
    e = emp(c, joining="2024-01-15")
    assert e["probation"]["status"] == "review_due" and e["probation"]["end_date"] == "2024-07-15"
    confirmed = c.post(f"/people/employees/{e['id']}/probation/confirm", {"confirmed_on": "2024-07-20"}).json()
    assert confirmed["probation"]["status"] == "confirmed" and confirmed["probation"]["confirmed_on"] == "2024-07-20"
    assert c.post(f"/people/employees/{e['id']}/probation/confirm", {}).status_code == 422  # only once
    assert c.post(f"/people/employees/{e['id']}/probation/extend", {"new_end_date": "2030-01-01", "reason": "late"}).status_code == 422  # not after confirming


def test_probation_can_be_extended_with_a_reason_on_record(c):
    e = emp(c, joining="2026-08-03")
    assert e["probation"]["end_date"] == "2027-02-03"
    done = c.post(f"/people/employees/{e['id']}/probation/extend", {"new_end_date": "2027-05-03", "reason": "Performance needs improving"}).json()
    assert done["probation"]["end_date"] == "2027-05-03"
    assert done["probation"]["extensions"][0]["from_date"] == "2027-02-03" and done["probation"]["extensions"][0]["reason"] == "Performance needs improving"
    for bad in ({"new_end_date": "2027-05-03", "reason": "again"}, {"new_end_date": "2027-04-01", "reason": "earlier"}, {"new_end_date": "2027-06-01", "reason": ""},
                {"new_end_date": "2027-06-01", "reason": "ab"}, {"new_end_date": "2029-01-01", "reason": "too long"}, {"new_end_date": "x", "reason": "bad date"}):
        assert c.post(f"/people/employees/{e['id']}/probation/extend", bad).status_code == 422, bad
    assert len(c.get(f"/people/employees/{e['id']}").json()["probation"]["extensions"]) == 1


def test_correcting_the_joining_date_moves_probation_unless_hr_changed_it(c):
    a = emp(c, joining="2026-08-03")
    assert c.patch(f"/people/employees/{a['id']}", {"joining_date": "2026-09-01"}).json()["probation"]["end_date"] == "2027-03-01"
    b = emp(c, name="Ben", joining="2026-08-03")
    c.post(f"/people/employees/{b['id']}/probation/extend", {"new_end_date": "2027-06-01", "reason": "Needs more time"})
    assert c.patch(f"/people/employees/{b['id']}", {"joining_date": "2026-09-01"}).json()["probation"]["end_date"] == "2027-06-01"


def test_people_from_before_probation_was_tracked_count_as_confirmed(c):
    from bson import ObjectId

    from sourcer.tenancy.db import tenant_db

    db = tenant_db("acme")
    old = emp(c, name="Legacy", joining="2020-02-01")
    db["employees"].update_one({"_id": ObjectId(old["id"])}, {"$unset": {"probation_end_date": "", "confirmed_on": "", "probation_extensions": ""}})
    assert c.get(f"/people/employees/{old['id']}").json()["probation"]["status"] == "confirmed"


# ---------- notice period ----------

def test_notice_is_seven_days_in_probation_and_two_months_after(c):
    probation = emp(c, name="New", joining="2026-08-03")
    n = c.post(f"/people/employees/{probation['id']}/notice", {"given_on": "2026-10-01", "note": "Better offer"}).json()["notice"]
    assert (n["basis"], n["last_working_day"], n["note"]) == ("probation", "2026-10-08", "Better offer")
    senior = emp(c, name="Senior", joining="2024-01-15")
    c.post(f"/people/employees/{senior['id']}/probation/confirm", {"confirmed_on": "2024-07-15"})
    n = c.post(f"/people/employees/{senior['id']}/notice", {"given_on": "2026-12-31"}).json()["notice"]
    assert (n["basis"], n["last_working_day"]) == ("notice_period", "2027-02-28")  # two months, clamped to the month's end


def test_not_yet_confirmed_still_means_the_short_notice(c):
    e = emp(c, joining="2024-01-15")  # probation ended long ago but nobody confirmed them
    assert c.post(f"/people/employees/{e['id']}/notice", {"given_on": "2026-10-01"}).json()["notice"]["basis"] == "probation"


def test_notice_given_before_confirmation_uses_the_probation_rule(c):
    e = emp(c, joining="2024-01-15")
    c.post(f"/people/employees/{e['id']}/probation/confirm", {"confirmed_on": "2026-10-10"})
    assert c.post(f"/people/employees/{e['id']}/notice", {"given_on": "2026-10-05"}).json()["notice"]["basis"] == "probation"


def test_notice_follows_this_companys_policy(c):
    c.patch("/people/policy", {"notice_period": {"amount": 1, "unit": "months"}, "probation_notice_period": {"amount": 14, "unit": "days"}})
    a = emp(c, name="Probie", joining="2026-08-03")
    assert c.post(f"/people/employees/{a['id']}/notice", {"given_on": "2026-10-01"}).json()["notice"]["last_working_day"] == "2026-10-15"
    b = emp(c.other, name="Probie2", joining="2026-08-03")
    assert c.other.post(f"/people/employees/{b['id']}/notice", {"given_on": "2026-10-01"}).json()["notice"]["last_working_day"] == "2026-10-08"  # the default seven days


def test_notice_can_be_extended_at_the_employees_request_and_keeps_its_history(c):
    e = emp(c, joining="2026-08-03")
    c.post(f"/people/employees/{e['id']}/notice", {"given_on": "2026-10-01"})
    done = c.patch(f"/people/employees/{e['id']}/notice", {"last_working_day": "2026-10-31", "reason": "Asked to finish a handover", "requested_by": "employee"}).json()["notice"]
    assert done["last_working_day"] == "2026-10-31"
    assert done["adjustments"] == [{"from_date": "2026-10-08", "to_date": "2026-10-31", "reason": "Asked to finish a handover", "requested_by": "employee", "at": done["adjustments"][0]["at"]}]
    again = c.patch(f"/people/employees/{e['id']}/notice", {"last_working_day": "2026-10-20", "reason": "Agreed to leave earlier", "requested_by": "company"}).json()["notice"]
    assert len(again["adjustments"]) == 2 and again["last_working_day"] == "2026-10-20"
    for bad in ({"last_working_day": "2026-10-20", "reason": "same day", "requested_by": "company"}, {"last_working_day": "2026-09-30", "reason": "before notice", "requested_by": "company"},
                {"last_working_day": "2026-11-01", "reason": "", "requested_by": "company"}, {"last_working_day": "2026-11-01", "reason": "who asked", "requested_by": "boss"}):
        assert c.patch(f"/people/employees/{e['id']}/notice", bad).status_code == 422, bad


def test_notice_rules(c):
    e = emp(c, joining="2026-08-03")
    assert c.patch(f"/people/employees/{e['id']}/notice", {"last_working_day": "2026-11-01", "reason": "none yet", "requested_by": "company"}).status_code == 422
    assert c.delete(f"/people/employees/{e['id']}/notice").status_code == 422
    assert c.post(f"/people/employees/{e['id']}/notice", {"given_on": "2026-07-01"}).status_code == 422  # before they joined
    assert c.post(f"/people/employees/{e['id']}/notice", {"given_on": "2026-02-30"}).status_code == 422
    assert c.post(f"/people/employees/{e['id']}/notice", {"given_on": "2026-10-01", "note": "x" * 301}).status_code == 422
    assert c.post(f"/people/employees/{e['id']}/notice", {"given_on": "2026-10-01"}).status_code == 201
    assert c.post(f"/people/employees/{e['id']}/notice", {"given_on": "2026-10-02"}).status_code == 422  # one notice at a time
    withdrawn = c.delete(f"/people/employees/{e['id']}/notice").json()
    assert withdrawn["notice"] is None
    c.patch(f"/people/employees/{e['id']}", {"status": "inactive"})
    assert c.post(f"/people/employees/{e['id']}/notice", {"given_on": "2026-10-01"}).status_code == 422  # already left


# ---------- access, isolation, audit ----------

@pytest.fixture
def staff(two_tenants, hr):
    from sourcer.tenancy import registry

    out = {}
    for role in ("recruiter", "employee"):
        user, first = registry.create_user(f"{role}@acme.com", "acme", role)
        registry.change_password(user["email"], first, "a-long-enough-password")
        out[role] = registry.get_user_by_id(user["_id"])
    return out


def test_only_hr_administrators_reach_any_of_it(api, c, staff):
    e = emp(c)
    r = leave(c, e, "PL", "2026-03-02").json()
    calls = [
        ("get", "/people/policy"), ("patch", "/people/policy"), ("get", "/people/leave-types"), ("post", "/people/leave-types"), ("get", "/people/holidays"),
        ("post", "/people/holidays"), ("get", "/people/leave"), ("post", "/people/leave"), ("post", "/people/leave/preview"), ("get", f"/people/leave/{r['id']}"),
        ("patch", f"/people/leave/{r['id']}"), ("post", f"/people/leave/{r['id']}/cancel"), ("get", f"/people/employees/{e['id']}/balances"),
        ("get", f"/people/employees/{e['id']}/leave-adjustments"), ("post", f"/people/employees/{e['id']}/leave-adjustments"),
        ("post", f"/people/employees/{e['id']}/probation/extend"), ("post", f"/people/employees/{e['id']}/probation/confirm"),
        ("post", f"/people/employees/{e['id']}/notice"), ("patch", f"/people/employees/{e['id']}/notice"), ("delete", f"/people/employees/{e['id']}/notice"),
    ]
    for who in ("recruiter", "employee"):
        for method, path in calls:
            res = api.request(method.upper(), path, headers=H("acme", staff[who]), json={} if method != "get" else None)
            assert res.status_code == 403, (who, method, path, res.status_code)
    assert c.get("/people/policy").status_code == 200  # and HR still can


def test_one_company_cannot_see_or_touch_anothers_leave(c):
    e = emp(c)
    r = leave(c, e, "PL", "2026-03-02").json()
    other = c.other
    assert other.get(f"/people/leave/{r['id']}").status_code == 404
    assert other.patch(f"/people/leave/{r['id']}", {"note": "x"}).status_code == 404
    assert other.post(f"/people/leave/{r['id']}/cancel", {}).status_code == 404
    assert other.get("/people/leave").json()["total"] == 0
    assert other.get(f"/people/employees/{e['id']}/balances").status_code == 404
    assert other.post(f"/people/employees/{e['id']}/notice", {"given_on": "2026-10-01"}).status_code == 404
    assert other.post("/people/leave", {"employee_id": e["id"], "type_id": types(other)["PL"]["id"], "start_date": "2026-03-02"}).status_code == 404
    # a leave type from another company is not a type here
    assert other.post("/people/leave", {"employee_id": emp(other)["id"], "type_id": types(c)["PL"]["id"], "start_date": "2026-03-02"}).status_code == 404


def test_the_log_records_what_happened_without_the_personal_text(c):
    from sourcer.tenancy.db import tenant_db

    e = emp(c)
    r = leave(c, e, "PL", "2026-03-02", reason="Medical appointment", note="Private note").json()
    c.post(f"/people/leave/{r['id']}/cancel", {"reason": "Cancelled for private reasons"})
    c.post(f"/people/employees/{e['id']}/notice", {"given_on": "2026-10-01", "note": "Secret reason"})
    log = list(tenant_db("acme")["audit_log"].find())
    actions = {a["action"] for a in log}
    assert {"leave_recorded", "leave_cancelled", "notice_recorded"} <= actions
    text = str(log)
    for secret in ("Medical", "Private note", "private reasons", "Secret reason"):
        assert secret not in text


def test_new_indexes_reach_existing_companies(two_tenants):
    from sourcer import admin
    from sourcer.tenancy.db import tenant_db

    admin.main(["indexes", "ensure", "--all"])
    assert any(i.get("unique") for i in tenant_db("acme")["leave_types"].list_indexes() if i["name"] == "code_1")
    assert any(i.get("unique") for i in tenant_db("acme")["holidays"].list_indexes() if i["name"] == "date_1")
