import pytest
from test_api_phase1 import H, ROLE, ROLE_DATA, seeded  # noqa: F401  (seeded is a fixture)

NEW = {
    "name": "react-dev-pune", "languages": ["JavaScript", "TypeScript"], "keywords": ["react", "NextJS"], "locations": ["Pune", "Mumbai"],
    "exclude_locations": ["Pakistan"], "min_years": 3, "max_years": 6, "min_public_repos": 3, "active_within_months": 12,
    "min_language_share": 0.4, "min_keyword_repos": 1, "role_title": "React Developer",
}


# ---------- the role builder ----------

def test_creating_a_role_and_reading_it_back(api, seeded):
    h = H(seeded)
    made = api.post("/roles", json={**NEW, "budget": {"min": 10, "max": 18, "stretch_max": 21}}, headers=h)
    assert made.status_code == 201
    body = made.json()
    assert body["keywords"] == ["react", "nextjs"]  # lower-cased
    assert body["budget"] == {"min": 10, "max": 18, "stretch_max": 21} and body["role_title"] == "React Developer" and body["candidates"] == 0
    assert api.get("/roles/react-dev-pune", headers=h).json()["languages"] == ["JavaScript", "TypeScript"]
    assert "react-dev-pune" in [r["name"] for r in api.get("/roles", headers=h).json()]
    assert api.get("/roles/react-dev-pune", headers=H(seeded, "globex")).status_code == 404  # another company never sees it


def test_a_role_name_must_be_new_and_url_safe(api, seeded):
    h = H(seeded)
    assert api.post("/roles", json=NEW, headers=h).status_code == 201
    assert api.post("/roles", json=NEW, headers=h).status_code == 409
    for bad in ("", "ab", "React Dev", "UPPER", "has_underscore", "a/b", "a:b", "-leading", "trailing-", "x" * 60):
        assert api.post("/roles", json={**NEW, "name": bad}, headers=h).status_code == 422, bad


@pytest.mark.parametrize("change", [
    {"languages": []}, {"locations": []}, {"min_years": 7, "max_years": 3}, {"max_years": 99}, {"min_language_share": 1.5},
    {"min_public_repos": -1}, {"active_within_months": 0}, {"keywords": [f"k{i}" for i in range(16)]}, {"languages": ["a", "b", "c", "d", "e", "f"]},
    {"locations": ["x" * 61]}, {"min_keyword_repos": 50},
])
def test_unreasonable_rules_are_refused(api, seeded, change):
    assert api.post("/roles", json={**NEW, **change}, headers=H(seeded)).status_code == 422
    assert "react-dev-pune" not in [r["name"] for r in api.get("/roles", headers=H(seeded)).json()]


def test_lists_are_tidied(api, seeded):
    made = api.post("/roles", json={**NEW, "keywords": ["  React ", "react", "", "Next  JS"], "locations": ["Pune", " pune ", "Mumbai"]}, headers=H(seeded)).json()
    assert made["keywords"] == ["react", "next js"] and made["locations"] == ["Pune", "Mumbai"]  # trimmed, de-duplicated, blanks dropped


def test_a_bad_budget_leaves_no_half_created_role(api, seeded):
    res = api.post("/roles", json={**NEW, "budget": {"max": 10, "stretch_max": 5}}, headers=H(seeded))
    assert res.status_code == 422
    assert api.get("/roles/react-dev-pune", headers=H(seeded)).status_code == 404


def test_editing_a_role_keeps_what_the_builder_does_not_show(api, seeded):
    from sourcer.tenancy.db import tenant_db

    h = H(seeded)
    stored = tenant_db("acme")["roles"].find_one({"_id": ROLE})["data"]
    assert stored["outreach"]["subject"]  # the YAML-era subject, body and weights are not in the builder
    changed = api.put(f"/roles/{ROLE}", json={**api.get(f"/roles/{ROLE}", headers=h).json(), "max_years": 8, "role_title": "Senior PHP Developer", "name": "ignored"}, headers=h)
    assert changed.status_code == 200 and changed.json()["max_years"] == 8 and changed.json()["name"] == ROLE  # the name cannot change
    after = tenant_db("acme")["roles"].find_one({"_id": ROLE})["data"]
    assert after["outreach"]["subject"] == stored["outreach"]["subject"] and after["outreach"]["role_title"] == "Senior PHP Developer"
    assert api.put("/roles/ghost", json=NEW, headers=h).status_code == 404
    assert api.put(f"/roles/{ROLE}", json={**NEW, "name": ROLE}, headers=H(seeded, "globex")).status_code == 200  # globex has its own copy
    assert tenant_db("acme")["roles"].find_one({"_id": ROLE})["data"]["experience"]["max_years"] == 8  # and that did not touch acme's


def test_the_budget_in_an_edit_is_set_cleared_or_left_alone(api, seeded):
    h = H(seeded)
    base = {k: v for k, v in api.get(f"/roles/{ROLE}", headers=h).json().items() if k not in ("budget", "candidates")}
    assert api.put(f"/roles/{ROLE}", json={**base, "budget": {"max": 15}}, headers=h).json()["budget"]["max"] == 15
    assert api.put(f"/roles/{ROLE}", json=base, headers=h).json()["budget"]["max"] == 15  # left out: unchanged
    assert api.put(f"/roles/{ROLE}", json={**base, "budget": None}, headers=h).json()["budget"] is None  # null: removed


# ---------- running sourcing ----------

@pytest.fixture
def ready(seeded):
    from sourcer.tenancy import registry

    registry.set_github_token("acme", "ghp_fake_for_tests")
    return seeded


def test_starting_a_run_queues_it(api, ready):
    h = H(ready)
    res = api.post("/jobs", json={"role": ROLE, "pages": 3, "max_candidates": 100, "remove_stale": True}, headers=h)
    assert res.status_code == 201
    job = res.json()
    assert (job["status"], job["phase"], job["pages"], job["max_candidates"], job["remove_stale"]) == ("queued", "waiting", 3, 100, True)
    assert job["created_by"] == "hr@acme.com" and job["total"] == 0 and job["started_at"] is None
    detail = api.get(f"/jobs/{job['id']}", headers=h).json()
    assert detail["events"][0]["message"] == "Queued by hr@acme.com"
    from sourcer.tenancy.db import tenant_db
    assert tenant_db("acme")["audit_log"].find_one({"action": "job_created"})["detail"]["role"] == ROLE


def test_a_run_needs_a_known_role_a_github_token_and_no_other_active_run(api, ready, seeded):
    h = H(ready)
    assert api.post("/jobs", json={"role": "ghost"}, headers=h).status_code == 404
    first = api.post("/jobs", json={"role": ROLE}, headers=h)
    assert first.status_code == 201
    assert api.post("/jobs", json={"role": ROLE}, headers=h).status_code == 409  # one at a time per role
    no_token = api.post("/jobs", json={"role": ROLE}, headers=H(seeded, "globex"))  # globex has no token
    assert no_token.status_code == 409 and "GitHub token" in no_token.json()["detail"]


@pytest.mark.parametrize("bad", [{"pages": 0}, {"pages": 6}, {"max_candidates": -1}, {"max_candidates": 5001}, {}])
def test_run_settings_are_bounded(api, ready, bad):
    assert api.post("/jobs", json={"role": ROLE, **bad} if bad else {}, headers=H(ready)).status_code == 422


def test_pause_resume_and_cancel_through_the_api(api, ready):
    h = H(ready)
    job = api.post("/jobs", json={"role": ROLE}, headers=h).json()
    url = f"/jobs/{job['id']}"
    assert api.post(f"{url}/pause", headers=h).json()["status"] == "paused"
    assert api.post(f"{url}/pause", headers=h).json()["status"] == "paused"  # asking again is harmless
    assert api.post(f"{url}/resume", headers=h).json()["status"] == "queued"
    assert api.post(f"{url}/resume", headers=h).status_code == 409  # a queued run cannot be resumed
    assert api.post(f"{url}/cancel", headers=h).json()["status"] == "cancelled"
    assert api.post(f"{url}/pause", headers=h).status_code == 409
    assert api.post(f"{url}/cancel", headers=h).status_code == 409
    assert api.post("/jobs", json={"role": ROLE}, headers=h).status_code == 201  # once over, a new run may start


def test_asking_a_running_job_to_pause_is_a_request_the_worker_answers(api, ready):
    from sourcer import jobs
    from sourcer.tenancy.db import tenant_db

    h = H(ready)
    job = api.post("/jobs", json={"role": ROLE}, headers=h).json()
    jobs.claim_next(tenant_db("acme"), "w")
    paused = api.post(f"/jobs/{job['id']}/pause", headers=h).json()
    assert paused["status"] == "running" and paused["control"] == "pause"  # not paused yet: the worker does that at a safe point
    cancelled = api.post(f"/jobs/{job['id']}/cancel", headers=h).json()
    assert cancelled["control"] == "cancel"


def test_listing_and_isolation(api, ready):
    h = H(ready)
    a = api.post("/jobs", json={"role": ROLE}, headers=h).json()
    assert [j["id"] for j in api.get("/jobs", headers=h).json()] == [a["id"]]
    assert api.get("/jobs?active=true", headers=h).json()[0]["id"] == a["id"]
    api.post(f"/jobs/{a['id']}/cancel", headers=h)
    assert api.get("/jobs?active=true", headers=h).json() == []
    other = H(ready, "globex")
    assert api.get("/jobs", headers=other).json() == []
    for path in ("", "/pause", "/resume", "/cancel"):
        method = api.get if path == "" else api.post
        assert method(f"/jobs/{a['id']}{path}", headers=other).status_code == 404, path
    assert api.get("/jobs/not-an-id", headers=h).status_code == 404
