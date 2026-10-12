import pytest
from fake_github import FakeGitHub, universe
from test_api_phase1 import H, ROLE, ROLE_DATA, cand, seeded  # noqa: F401  (seeded is a fixture)

BLR = ["Bengaluru", "Bangalore"]


def put(api, seeded, tenant="acme", **change):
    h = H(seeded, tenant)
    base = {k: v for k, v in api.get(f"/roles/{ROLE}", headers=h).json().items() if k not in ("budget", "candidates")}
    return api.put(f"/roles/{ROLE}", json={**base, **change}, headers=h)


def order(api, seeded, qs="", tenant="acme"):
    return [i["login"] for i in api.get(f"/candidates?role={ROLE}{qs}", headers=H(seeded, tenant)).json()["items"]]


@pytest.fixture
def mixed(seeded):
    """Four people at different scores and places. ann (90) is in Pune; the others are in Bengaluru, Bangalore and Hyderabad."""
    from sourcer.tenancy.db import tenant_db

    col = tenant_db("acme")["candidates"]
    col.delete_many({})
    col.insert_many([
        cand("ann", 90, location="Pune, India"),
        cand("bob", 60, location="Bengaluru, Karnataka"),
        cand("cy", 40, location="BANGALORE, India"),
        cand("dee", 70, location="Hyderabad, India"),
    ])
    return seeded


# ---------- the setting ----------

def test_the_setting_round_trips_and_is_tidied(api, seeded):
    res = put(api, seeded, preferred_locations=["  Bengaluru ", "bengaluru", "", "Bangalore"])
    assert res.status_code == 200 and res.json()["preferred_locations"] == ["Bengaluru", "Bangalore"]
    assert api.get(f"/roles/{ROLE}", headers=H(seeded)).json()["preferred_locations"] == BLR
    assert [r["preferred_locations"] for r in api.get("/roles", headers=H(seeded)).json()] == [BLR]  # the list shows it too


def test_too_many_or_too_long_places_are_refused(api, seeded):
    assert put(api, seeded, preferred_locations=[f"p{i}" for i in range(11)]).status_code == 422
    assert put(api, seeded, preferred_locations=["x" * 61]).status_code == 422


def test_it_is_per_company(api, seeded):
    put(api, seeded, preferred_locations=BLR)
    assert api.get(f"/roles/{ROLE}", headers=H(seeded, "globex")).json()["preferred_locations"] == []


# ---------- ordering ----------

def test_preferred_people_come_first_then_by_score(api, mixed):
    assert order(api, mixed) == ["ann", "dee", "bob", "cy"]  # before the setting: plain score order
    put(api, mixed, preferred_locations=BLR)
    assert order(api, mixed) == ["bob", "cy", "ann", "dee"]  # Bengaluru and Bangalore (any case) first, each group by score


def test_other_sorts_are_exactly_what_was_asked_for(api, mixed):
    put(api, mixed, preferred_locations=BLR)
    assert order(api, mixed, "&sort=name&order=asc") == ["ann", "bob", "cy", "dee"]
    assert order(api, mixed, "&sort=score&order=asc") == ["cy", "bob", "dee", "ann"]  # ascending is not "preferred first"
    assert order(api, mixed, "&sort=est_years&order=desc&limit=1") != []  # no error from sorting on a field some rows lack


def test_the_list_says_who_is_preferred_and_filters_still_apply(api, mixed):
    put(api, mixed, preferred_locations=BLR)
    items = {i["login"]: i["preferred"] for i in api.get(f"/candidates?role={ROLE}", headers=H(mixed)).json()["items"]}
    assert items == {"ann": False, "bob": True, "cy": True, "dee": False}
    assert order(api, mixed, "&min_score=65") == ["dee", "ann"] or order(api, mixed, "&min_score=65") == ["ann", "dee"]
    assert order(api, mixed, "&q=bengaluru") == ["bob"]


def test_changing_or_clearing_the_setting_reflags_people_already_found(api, mixed):
    put(api, mixed, preferred_locations=BLR)
    assert order(api, mixed)[:2] == ["bob", "cy"]
    put(api, mixed, preferred_locations=["Hyderabad"])
    assert order(api, mixed)[0] == "dee"  # now Hyderabad leads
    put(api, mixed, preferred_locations=[])
    assert order(api, mixed) == ["ann", "dee", "bob", "cy"]
    assert not any(i["preferred"] for i in api.get(f"/candidates?role={ROLE}", headers=H(mixed)).json()["items"])


def test_reflagging_never_touches_another_company_or_role(api, mixed):
    from sourcer.tenancy.db import tenant_db

    tenant_db("acme")["candidates"].insert_one(cand("other-role", 99, role="a-different-role", _id="a-different-role:other-role", location="Bengaluru"))
    put(api, mixed, preferred_locations=BLR)
    assert tenant_db("acme")["candidates"].find_one({"login": "other-role"}).get("preferred") is None
    assert [c.get("preferred") for c in tenant_db("globex")["candidates"].find({})] == [None]  # globex's candidate was never flagged


@pytest.mark.parametrize("term", ["(", "C++", ".*", "[a-", "\\"])
def test_a_place_name_is_text_not_a_pattern(api, mixed, term):
    from sourcer.tenancy.db import tenant_db

    assert put(api, mixed, preferred_locations=[term]).status_code == 200  # nothing blows up
    flagged = [d["login"] for d in tenant_db("acme")["candidates"].find({"preferred": True})]
    assert flagged == []  # ".*" must not match everyone


def test_export_lists_preferred_people_first(api, mixed):
    import csv
    import io

    put(api, mixed, preferred_locations=BLR)
    for login in ("ann", "bob", "cy", "dee"):
        from sourcer.tenancy.db import tenant_db
        tenant_db("acme")["candidates"].update_one({"login": login}, {"$set": {"email": f"{login}@x.com"}})
    res = api.get(f"/export?role={ROLE}&format=csv", headers=H(mixed))
    assert [r["login"] for r in csv.DictReader(io.StringIO(res.content.decode("utf-8-sig")))] == ["bob", "cy", "ann", "dee"]


# ---------- people found by a run ----------

def test_a_sourcing_run_flags_preferred_people_as_it_saves_them(mongo, monkeypatch):
    from sourcer import jobs, pipeline, roles_store
    from sourcer.tenancy import registry
    from sourcer.tenancy.db import tenant_db

    registry.create_tenant("acme", "Acme")
    db = tenant_db("acme")
    roles_store.import_role(db, {**ROLE_DATA, "preferred_locations": BLR})
    jobs.create(db, ROLE, {}, "t")
    job = jobs.claim_next(db, "w")
    site = lambda url: {"portfolio_url": None, "linkedin": None, "emails": [], "resumes": [], "phones": []}
    pipeline.run_job(db, job, "tok", client_factory=lambda token, **kw: FakeGitHub(universe(), fail={"flaky"}, **kw), crawl=site)
    flags = {d["login"]: d["preferred"] for d in db["candidates"].find({})}
    # amy and chitra live in Bengaluru; ben is in Pune and dev in Chennai.
    assert flags == {"amy": True, "ben": False, "chitra": True, "dev": False}


def test_evaluate_matches_places_the_way_the_filter_does():
    from sourcer.enrich import is_preferred

    assert is_preferred("Bengaluru, India", BLR) and is_preferred("bangalore", BLR) and is_preferred("NEAR BANGALORE", BLR)
    assert not is_preferred("Pune", BLR) and not is_preferred(None, BLR) and not is_preferred("Pune", []) and not is_preferred("Pune", ["  "])
