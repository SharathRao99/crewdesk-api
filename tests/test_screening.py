import csv
import io
import threading
from datetime import datetime, timedelta, timezone

import pytest
from test_api_phase1 import H, KEY, ROLE, cand, seeded, tpl  # noqa: F401  (seeded is a fixture)

PUB = {"X-Service-Key": KEY}
GOOD = {"current_ctc": 12.5, "expected_ctc": 16, "notice_days": 30, "comment": "Open to Bangalore", "consent": True}


def now():
    return datetime.now(timezone.utc)


def new_link(slug="acme", login="ann", **overrides):
    """Insert a link directly (so tests can make expired or used ones) and return (link, token)."""
    from sourcer import screening
    from sourcer.tenancy.db import tenant_db

    link = {"candidate_id": f"{ROLE}:{login}", "role": ROLE, "login": login, "created_by": "t", "created_at": now(),
            "expires_at": now() + timedelta(days=14), "used_at": None, "opened_at": None, "revoked": False, **overrides}
    tenant_db(slug)["screening_links"].insert_one(link)
    return link, screening.make_token(slug, link["_id"])


def url_token(url):
    return url.rsplit("/s/", 1)[1]


# ---------- tokens ----------

def test_token_roundtrip_and_tampering(mongo):
    from bson import ObjectId

    from sourcer import screening

    oid = ObjectId()
    token = screening.make_token("acme", oid)
    assert screening.parse_token(token) == ("acme", oid)
    slug, link_id, sig = token.split(".")
    for bad in (f"globex.{link_id}.{sig}", f"{slug}.{ObjectId()}.{sig}", f"{slug}.{link_id}.{sig[:-1]}x", f"{slug}.{link_id}", f"{token}.extra",
                "", "a.b.c", "x" * 500, f"..{sig}", f"ACME.{link_id}.{sig}"):
        assert screening.parse_token(bad) is None, bad


def test_token_is_invalid_under_another_key(mongo, monkeypatch):
    from bson import ObjectId

    from sourcer import screening
    from sourcer.tenancy import crypto

    token = screening.make_token("acme", ObjectId())
    monkeypatch.setenv("SECRETS_KEY", crypto.generate_key())
    assert screening.parse_token(token) is None


# ---------- links ----------

def test_open_link_is_reused_and_regenerating_cancels_the_old_one(seeded):
    from sourcer import screening
    from sourcer.tenancy.db import tenant_db

    db = tenant_db("acme")
    row = db["candidates"].find_one({"login": "ann"})
    first = screening.get_or_create_link(db, row, "t")
    assert screening.get_or_create_link(db, row, "t")["_id"] == first["_id"]  # same URL every time
    second = screening.regenerate_link(db, row, "t")
    assert second["_id"] != first["_id"]
    assert screening.resolve(screening.make_token("acme", first["_id"])) is None  # the old link is dead
    assert screening.resolve(screening.make_token("acme", second["_id"])) is not None


def test_expired_and_used_links_are_replaced_not_reused(seeded):
    from sourcer import screening
    from sourcer.tenancy.db import tenant_db

    db = tenant_db("acme")
    row = db["candidates"].find_one({"login": "ann"})
    expired, _ = new_link(expires_at=now() - timedelta(minutes=1))
    assert screening.link_state(expired) == "expired"
    fresh = screening.get_or_create_link(db, row, "t")
    assert fresh["_id"] != expired["_id"] and screening.link_state(fresh) == "open"
    db["screening_links"].update_one({"_id": fresh["_id"]}, {"$set": {"used_at": now()}})
    assert screening.get_or_create_link(db, row, "t")["_id"] != fresh["_id"]


# ---------- public form ----------

def test_public_state_reveals_only_what_the_candidate_needs(api, seeded):
    _, token = new_link()
    body = api.get(f"/public/screening/{token}", headers=PUB).json()
    assert body == {"state": "open", "first_name": "Ann", "role_title": "PHP Laravel Developer", "company_name": "Acme Inc",
                    "consent_version": "2026-10"}
    from sourcer.tenancy.db import tenant_db
    assert tenant_db("acme")["screening_links"].find_one({})["opened_at"] is not None


@pytest.mark.parametrize("make", [
    lambda: "garbage",
    lambda: new_link(revoked=True)[1],
    lambda: new_link()[1].replace("acme", "globex", 1),  # a token for one company presented as another's
    lambda: new_link()[1][:-2] + "xx",
])
def test_unknown_cancelled_or_tampered_links_all_look_the_same(api, seeded, make):
    token = make()
    assert api.get(f"/public/screening/{token}", headers=PUB).json() == {
        "state": "invalid", "first_name": None, "role_title": None, "company_name": None, "consent_version": None}
    assert api.post(f"/public/screening/{token}", json=GOOD, headers=PUB).status_code == 404


def test_expired_and_answered_links_name_only_the_company(api, seeded):
    _, expired = new_link(expires_at=now() - timedelta(days=1))
    assert api.get(f"/public/screening/{expired}", headers=PUB).json()["state"] == "expired"
    assert api.get(f"/public/screening/{expired}", headers=PUB).json()["first_name"] is None
    _, used = new_link(used_at=now())
    assert api.get(f"/public/screening/{used}", headers=PUB).json()["state"] == "answered"
    assert api.post(f"/public/screening/{expired}", json=GOOD, headers=PUB).status_code == 410


def test_public_routes_need_the_service_key(api, seeded):
    _, token = new_link()
    assert api.get(f"/public/screening/{token}").status_code == 401
    assert api.post(f"/public/screening/{token}", json=GOOD).status_code == 401
    assert api.get(f"/public/screening/{token}", headers={"X-Service-Key": "wrong"}).status_code == 401


def test_submitting_stores_answers_updates_the_candidate_and_uses_up_the_link(api, seeded):
    from sourcer.tenancy.db import tenant_db

    _, token = new_link(login="bob")
    assert api.post(f"/public/screening/{token}", json=GOOD, headers=PUB).status_code == 201
    db = tenant_db("acme")
    stored = db["screening_responses"].find_one({})
    assert (stored["current_ctc"], stored["expected_ctc"], stored["notice_days"], stored["consent"]) == (12.5, 16, 30, True)
    assert stored["consent_version"] == "2026-10" and "ip" not in str(stored)
    bob = db["candidates"].find_one({"login": "bob"})
    assert bob["screening"]["expected_ctc"] == 16 and bob["status"] == "replied"
    event = db["candidate_events"].find_one({"login": "bob"})
    assert (event["from_status"], event["to_status"], event["by"]) == ("new", "replied", "candidate (form)")
    assert api.post(f"/public/screening/{token}", json=GOOD, headers=PUB).status_code == 409  # once only
    assert api.get(f"/public/screening/{token}", headers=PUB).json()["state"] == "answered"


@pytest.mark.parametrize("start,expect", [("new", "replied"), ("contacted", "replied"), ("interviewing", "interviewing"), ("hired", "hired")])
def test_answering_only_moves_early_statuses_to_replied(api, seeded, start, expect):
    from sourcer.tenancy.db import tenant_db

    tenant_db("acme")["candidates"].update_one({"login": "bob"}, {"$set": {"status": start}})
    _, token = new_link(login="bob")
    assert api.post(f"/public/screening/{token}", json=GOOD, headers=PUB).status_code == 201
    assert tenant_db("acme")["candidates"].find_one({"login": "bob"})["status"] == expect


@pytest.mark.parametrize("change", [
    {"consent": False}, {"current_ctc": -1}, {"expected_ctc": 501}, {"notice_days": 400}, {"notice_days": -5},
    {"comment": "x" * 501}, {"expected_ctc": "lots"}, {"consent": "yes please", "current_ctc": None},
])
def test_invalid_answers_are_refused_and_keep_the_link_open(api, seeded, change):
    from sourcer.tenancy.db import tenant_db

    _, token = new_link()
    assert api.post(f"/public/screening/{token}", json={**GOOD, **change}, headers=PUB).status_code == 422
    assert tenant_db("acme")["screening_responses"].count_documents({}) == 0
    assert api.get(f"/public/screening/{token}", headers=PUB).json()["state"] == "open"


def test_honeypot_pretends_to_work_but_keeps_nothing(api, seeded):
    from sourcer.tenancy.db import tenant_db

    _, token = new_link()
    res = api.post(f"/public/screening/{token}", json={**GOOD, "website": "http://spam.example"}, headers=PUB)
    assert res.status_code == 201 and res.json() == {"ok": True}
    assert tenant_db("acme")["screening_responses"].count_documents({}) == 0
    assert api.get(f"/public/screening/{token}", headers=PUB).json()["state"] == "open"  # a real person can still answer


def test_two_simultaneous_submissions_cannot_both_succeed(api, seeded):
    from sourcer.tenancy.db import tenant_db

    _, token = new_link()
    codes, gate = [], threading.Barrier(6)

    def go(i):
        gate.wait()
        codes.append(api.post(f"/public/screening/{token}", json=GOOD, headers={**PUB, "X-Client-IP": f"10.0.0.{i}"}).status_code)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [201, 409, 409, 409, 409, 409]
    assert tenant_db("acme")["screening_responses"].count_documents({}) == 1


def test_rate_limits_by_token_and_by_visitor(api, seeded):
    _, token = new_link()
    spam = {**GOOD, "website": "bot"}  # counted, but stores nothing
    codes = [api.post(f"/public/screening/{token}", json=spam, headers={**PUB, "X-Client-IP": f"1.1.1.{i}"}).status_code for i in range(12)]
    assert codes[:10] == [201] * 10 and codes[10:] == [429, 429]  # 10 per token per hour, whoever asks

    codes = [api.get("/public/screening/nope", headers={**PUB, "X-Client-IP": "9.9.9.9"}).status_code for _ in range(62)]
    assert codes[:60] == [200] * 60 and codes[60:] == [429, 429]  # 60 looks per minute per visitor
    assert api.get("/public/screening/nope", headers={**PUB, "X-Client-IP": "8.8.8.8"}).status_code == 200  # others unaffected


def test_a_deleted_candidate_invalidates_their_link(api, seeded):
    from sourcer.tenancy.db import tenant_db

    _, token = new_link()
    tenant_db("acme")["candidates"].delete_one({"login": "ann"})
    assert api.get(f"/public/screening/{token}", headers=PUB).json()["state"] == "invalid"


# ---------- recruiter side ----------

def test_screening_states_for_the_recruiter(api, seeded):
    h = H(seeded)
    assert api.get(f"/candidates/{ROLE}/ann/screening", headers=h).json()["state"] == "none"
    pending = api.post(f"/candidates/{ROLE}/ann/screening-link", headers=h).json()
    assert pending["state"] == "pending" and pending["link_url"].startswith("http://localhost:3100/s/")
    assert api.get(f"/candidates/{ROLE}/ann/screening", headers=h).json()["link_url"] == pending["link_url"]  # stable, not re-minted
    again = api.post(f"/candidates/{ROLE}/ann/screening-link", headers=h).json()
    assert again["link_url"] != pending["link_url"]
    assert api.get(f"/public/screening/{url_token(pending['link_url'])}", headers=PUB).json()["state"] == "invalid"  # the old one is cancelled
    assert api.get(f"/public/screening/{url_token(again['link_url'])}", headers=PUB).json()["state"] == "open"

    expired, _ = new_link(login="bob", expires_at=now() - timedelta(days=1))
    assert api.get(f"/candidates/{ROLE}/bob/screening", headers=h).json()["state"] == "expired"
    assert api.get(f"/candidates/{ROLE}/ghost/screening", headers=h).status_code == 404


def test_answers_and_budget_verdicts(api, seeded):
    h = H(seeded)
    answer = lambda login, expected: api.post(
        f"/public/screening/{url_token(api.post(f'/candidates/{ROLE}/{login}/screening-link', headers=h).json()['link_url'])}",
        json={**GOOD, "expected_ctc": expected}, headers=PUB)
    assert answer("ann", 12).status_code == 201
    out = api.get(f"/candidates/{ROLE}/ann/screening", headers=h).json()
    assert out["state"] == "answered" and out["answers"]["expected_ctc"] == 12 and out["verdict"] is None  # no budget yet

    assert api.put(f"/roles/{ROLE}/budget", json={"min": 8, "max": 15, "stretch_max": 18}, headers=h).json()["budget"] == {"min": 8, "max": 15, "stretch_max": 18}
    assert api.get(f"/candidates/{ROLE}/ann/screening", headers=h).json()["verdict"] == "within"  # judged on the current budget
    answer("bob", 17)
    answer("cy", 25)
    verdicts = {i["login"]: i["budget_verdict"] for i in api.get(f"/candidates?role={ROLE}", headers=h).json()["items"]}
    assert verdicts == {"ann": "within", "bob": "stretch", "cy": "over", "dee": None}
    api.put(f"/roles/{ROLE}/budget", json={"max": 30}, headers=h)  # raising the budget changes every verdict
    assert {i["login"]: i["budget_verdict"] for i in api.get(f"/candidates?role={ROLE}", headers=h).json()["items"]}["cy"] == "within"


def test_list_filters_and_sorting_by_answers(api, seeded):
    h = H(seeded)
    for login, expected in (("ann", 12), ("bob", 17), ("cy", 25)):
        link = api.post(f"/candidates/{ROLE}/{login}/screening-link", headers=h).json()["link_url"]
        api.post(f"/public/screening/{url_token(link)}", json={**GOOD, "expected_ctc": expected}, headers=PUB)
    api.put(f"/roles/{ROLE}/budget", json={"max": 15, "stretch_max": 18}, headers=h)
    get = lambda qs: [i["login"] for i in api.get(f"/candidates?role={ROLE}&{qs}", headers=h).json()["items"]]
    assert get("answered=true") == ["ann", "bob", "cy"] and get("answered=false") == ["dee"]
    assert get("budget=within") == ["ann"] and get("budget=stretch") == ["bob"] and get("budget=over") == ["cy"]
    assert get("sort=expected_ctc&order=asc&answered=true") == ["ann", "bob", "cy"]
    assert api.get("/candidates?budget=within", headers=h).status_code == 422  # a verdict needs one role's budget
    item = api.get(f"/candidates?role={ROLE}&q=ann", headers=h).json()["items"][0]
    assert item["screening"]["notice_days"] == 30


def test_no_budget_means_no_verdict_filter_results(api, seeded):
    h = H(seeded)
    link = api.post(f"/candidates/{ROLE}/ann/screening-link", headers=h).json()["link_url"]
    api.post(f"/public/screening/{url_token(link)}", json=GOOD, headers=PUB)
    assert api.get(f"/candidates?role={ROLE}&budget=within", headers=h).json()["total"] == 0


@pytest.mark.parametrize("budget", [{"max": 0}, {"max": 501}, {"min": 20, "max": 10}, {"max": 10, "stretch_max": 5}, {"min": -1, "max": 10}, {"max": 10, "stretch_max": 600}])
def test_bad_budgets_are_refused(api, seeded, budget):
    assert api.put(f"/roles/{ROLE}/budget", json=budget, headers=H(seeded)).status_code == 422
    assert api.get("/roles", headers=H(seeded)).json()[0]["budget"] is None


def test_budget_is_per_company_and_can_be_cleared(api, seeded):
    api.put(f"/roles/{ROLE}/budget", json={"max": 15}, headers=H(seeded))
    assert api.get("/roles", headers=H(seeded, "globex")).json()[0]["budget"] is None
    assert api.put("/roles/ghost/budget", json={"max": 15}, headers=H(seeded)).status_code == 404
    assert api.delete(f"/roles/{ROLE}/budget", headers=H(seeded)).json()["budget"] is None


def test_form_link_in_a_draft_is_one_stable_private_url(api, seeded):
    from sourcer.tenancy.db import tenant_db

    h = H(seeded)
    tid = api.post("/templates", json=tpl(subject="Hi", body="Please answer: {form_link}"), headers=h).json()["id"]
    first = api.post(f"/candidates/{ROLE}/ann/draft", json={"template_id": tid}, headers=h).json()
    second = api.post(f"/candidates/{ROLE}/ann/draft", json={"template_id": tid}, headers=h).json()
    assert first["body"] == second["body"] and "/s/acme." in first["body"]
    assert tenant_db("acme")["screening_links"].count_documents({}) == 1
    other = api.post(f"/candidates/{ROLE}/bob/draft", json={"template_id": tid}, headers=h).json()
    assert other["body"] != first["body"]  # every candidate gets their own link
    token = first["body"].rsplit("/s/", 1)[1]
    assert api.get(f"/public/screening/{token}", headers=PUB).json()["first_name"] == "Ann"


def test_previewing_a_template_never_creates_links(api, seeded):
    from sourcer.tenancy.db import tenant_db

    h = H(seeded)
    res = api.post("/templates/preview", json={"subject": "s", "body": "{form_link}", "role": ROLE, "login": "ann"}, headers=h).json()
    assert "this-is-a-preview-link" in res["body"] and res["unknown"] == []
    assert tenant_db("acme")["screening_links"].count_documents({}) == 0


def test_export_carries_form_links_and_answers(api, seeded):
    h = H(seeded)
    tid = api.post("/templates", json=tpl(subject="Hi", body="Form: {form_link}"), headers=h).json()["id"]
    api.put(f"/roles/{ROLE}/default-template", json={"template_id": tid}, headers=h)
    link = api.post(f"/candidates/{ROLE}/ann/screening-link", headers=h).json()["link_url"]
    api.post(f"/public/screening/{url_token(link)}", json=GOOD, headers=PUB)
    api.put(f"/roles/{ROLE}/budget", json={"max": 20}, headers=h)

    res = api.get(f"/export?role={ROLE}&format=csv", headers=h)
    rows = {r["login"]: r for r in csv.DictReader(io.StringIO(res.content.decode("utf-8-sig")))}
    assert set(rows) == {"ann", "bob"}
    assert rows["ann"]["expected_ctc"] == "16.0" and rows["ann"]["notice_days"] == "30" and rows["ann"]["budget_verdict"] == "within"
    assert rows["bob"]["expected_ctc"] == "" and "/s/acme." in rows["bob"]["email_message"]
    assert rows["ann"]["email_message"] != rows["bob"]["email_message"]
    from sourcer.tenancy.db import tenant_db
    assert tenant_db("acme")["audit_log"].find_one({"action": "export"})["detail"]["form_links"] == 2


# ---------- deleting people and old data ----------

def test_deleting_a_candidate_removes_everything_and_needs_an_admin(api, seeded):
    from sourcer.tenancy import registry
    from sourcer.tenancy.db import tenant_db

    h = H(seeded)
    link = api.post(f"/candidates/{ROLE}/ann/screening-link", headers=h).json()["link_url"]
    api.post(f"/public/screening/{url_token(link)}", json=GOOD, headers=PUB)
    api.patch(f"/candidates/{ROLE}/ann", json={"notes": "n"}, headers=h)

    recruiter, _ = registry.create_user("rec@acme.com", "acme", "recruiter")
    rh = {**h, "X-User-Id": str(recruiter["_id"])}
    assert api.delete(f"/candidates/{ROLE}/ann", headers=rh).status_code == 403
    assert api.get(f"/candidates/{ROLE}/ann", headers=h).status_code == 200  # still there

    res = api.delete(f"/candidates/{ROLE}/ann", headers=h)
    assert res.status_code == 200 and res.json()["candidates"] == 1 and res.json()["responses"] == 1 and res.json()["links"] == 1
    db = tenant_db("acme")
    for name in ("candidates", "candidate_events", "screening_links", "screening_responses"):
        assert db[name].count_documents({"$or": [{"login": "ann"}, {"_id": f"{ROLE}:ann"}, {"candidate_id": f"{ROLE}:ann"}]}) == 0, name
    assert db["audit_log"].find_one({"action": "delete_candidate"})["detail"]["login"] == "ann"
    assert api.delete(f"/candidates/{ROLE}/ann", headers=h).status_code == 404
    assert api.get(f"/public/screening/{url_token(link)}", headers=PUB).json()["state"] == "invalid"


def test_deleting_cannot_cross_companies(api, seeded):
    assert api.delete(f"/candidates/{ROLE}/bob", headers=H(seeded, "globex")).status_code == 404  # globex has no bob
    assert api.get(f"/candidates/{ROLE}/bob", headers=H(seeded)).status_code == 200


def test_retention_purges_old_answers_and_links_only(api, seeded):
    import argparse

    from sourcer import admin
    from sourcer.tenancy.db import tenant_db

    h = H(seeded)
    for login in ("ann", "bob"):
        link = api.post(f"/candidates/{ROLE}/{login}/screening-link", headers=h).json()["link_url"]
        api.post(f"/public/screening/{url_token(link)}", json=GOOD, headers=PUB)
    db = tenant_db("acme")
    old = now() - timedelta(days=200)
    db["screening_responses"].update_one({"login": "ann"}, {"$set": {"submitted_at": old}})
    db["screening_links"].update_one({"login": "ann"}, {"$set": {"created_at": old}})

    admin._retention_run(argparse.Namespace(tenant="acme", all=False, days=180))
    assert db["screening_responses"].count_documents({}) == 1 and db["screening_links"].count_documents({}) == 1
    assert "screening" not in db["candidates"].find_one({"login": "ann"})  # the summary goes with the answers
    assert "screening" in db["candidates"].find_one({"login": "bob"})
    assert api.get(f"/candidates/{ROLE}/ann/screening", headers=h).json()["state"] == "none"
    assert tenant_db("globex")["candidates"].count_documents({}) == 1  # other companies untouched
