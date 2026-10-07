from datetime import timedelta

import pytest
from fake_github import Crash, FakeGitHub, universe
from test_api_phase1 import ROLE, ROLE_DATA

NOW = None


def site(url):  # a pretend portfolio crawl
    return {"portfolio_url": f"https://{url}" if url else None, "linkedin": None, "emails": [], "resumes": [], "phones": []}


@pytest.fixture
def env(mongo, monkeypatch):
    from sourcer import pipeline, roles_store
    from sourcer.tenancy import registry
    from sourcer.tenancy.db import tenant_db

    monkeypatch.setattr(pipeline, "CHECKPOINT_EVERY", 2)  # tiny batches so crashes land mid-run
    monkeypatch.setattr(pipeline, "CONTROL_POLL_SECONDS", 0)  # notice a pause or cancel at once, not within a second
    registry.create_tenant("acme", "Acme Inc")
    db = tenant_db("acme")
    roles_store.import_role(db, ROLE_DATA)
    return db


def start(db, **params):
    from sourcer import jobs

    job = jobs.create(db, ROLE, params, "hr@acme.com")
    return jobs.claim_next(db, "w1")


def run(db, job, people=None, **client_kw):
    from sourcer import pipeline

    made = []
    client_kw.setdefault("fail", {"flaky"})  # this person's profile always errors

    def factory(token, **kw):
        client = FakeGitHub(people or universe(), **client_kw, **kw)
        made.append(client)
        return client

    status = pipeline.run_job(db, job, "token", client_factory=factory, crawl=site)
    return status, made


def logins(db):
    return sorted(d["login"] for d in db["candidates"].find({"role": ROLE}))


# ---------- the whole pipeline ----------

def test_a_run_finds_scores_and_saves_the_right_people(env):
    db = env
    job = start(db)
    status, _ = run(db, job)
    assert status == "done"
    # amy, ben, chitra, dev qualify. weak is not mainly PHP, tex and karachi are in the wrong place, flaky errors.
    assert logins(db) == ["amy", "ben", "chitra", "dev"]
    done = db["jobs"].find_one({})
    assert (done["kept"], done["not_primary"], done["dropped"], done["errors"]) == (4, 1, 2, 1)
    assert done["status"] == "done" and done["phase"] == "finished" and done["finished_at"] and done["cursor"] == done["total"] == 8
    assert done["contacts_total"] == done["contacts_done"] == 4
    amy = db["candidates"].find_one({"login": "amy"})
    assert amy["score"] > 50 and amy["portfolio_url"] == "https://amy.dev" and "contacts_checked_at" in amy
    assert any(e["message"].startswith("Skipped flaky") for e in db["job_events"].find({}))


def test_the_job_log_and_progress_are_readable(env):
    from sourcer import jobs

    db = env
    run(db, start(db))
    messages = [e["message"] for e in jobs.events(db, db["jobs"].find_one({})["_id"])]
    assert "Finished" in messages[0] and any("Found 8 possible people" in m for m in messages) and any(m.startswith("Queued by") for m in messages)


# ---------- crashes and restarts ----------

def test_a_killed_worker_resumes_without_redoing_saved_people(env):
    from sourcer import jobs

    db = env
    job = start(db)
    first = {}

    with pytest.raises(Crash):
        run(db, job, crash_after=5)  # the process dies while checking the sixth person
    stopped = db["jobs"].find_one({})
    assert stopped["status"] == "running" and stopped["logins"] and 0 < stopped["cursor"] < stopped["total"]  # left mid-run, place saved
    saved_before = logins(db)
    assert saved_before  # people checked before the crash are already in the database

    # The heartbeat goes stale; the next worker puts the job back in the queue and carries on.
    db["jobs"].update_one({}, {"$set": {"heartbeat_at": db["jobs"].find_one({})["heartbeat_at"] - timedelta(minutes=10)}})
    assert jobs.requeue_orphans(db) == 1
    resumed = jobs.claim_next(db, "w2")
    status, clients = run(db, resumed)
    assert status == "done"
    assert logins(db) == ["amy", "ben", "chitra", "dev"]

    repeated = [p for p in clients[0].calls if p.startswith("/users/") and p.count("/") == 2]
    checked_before_crash = stopped["cursor"]
    assert len(repeated) == stopped["total"] - checked_before_crash  # only the people after the saved place were checked again
    assert not any(c == "/search/users" for c in clients[0].calls)  # discovery was not repeated either
    del first


def test_a_fresh_heartbeat_is_not_treated_as_orphaned(env):
    from sourcer import jobs

    db = env
    start(db)
    assert jobs.requeue_orphans(db) == 0
    assert db["jobs"].find_one({})["status"] == "running"


def test_a_worker_shutting_down_hands_the_job_back(env):
    from sourcer import pipeline

    db = env
    job = start(db)
    calls = {"n": 0}

    def stopping():
        calls["n"] += 1
        return calls["n"] > 30  # the signal arrives part-way through

    status = pipeline.run_job(db, job, "t", client_factory=lambda token, **kw: FakeGitHub(universe(), **kw), crawl=site, stopping=stopping)
    assert status == "queued"
    left = db["jobs"].find_one({})
    assert left["status"] == "queued" and left["control"] is None


# ---------- pause, resume, cancel ----------

def test_pausing_stops_at_a_safe_point_and_resuming_finishes_the_work(env):
    from sourcer import jobs

    db = env
    job = start(db)
    status, _ = run(db, job, pause_after=3, on_pause=lambda: jobs.pause(db, db["jobs"].find_one({})))
    assert status == "paused"
    paused = db["jobs"].find_one({})
    assert paused["status"] == "paused" and paused["control"] is None and 0 < paused["cursor"] < paused["total"] and paused["finished_at"] is None

    jobs.resume(db, db["jobs"].find_one({}))
    status, clients = run(db, jobs.claim_next(db, "w1"))
    assert status == "done" and logins(db) == ["amy", "ben", "chitra", "dev"]
    checked_again = [p for p in clients[0].calls if p.startswith("/users/") and p.count("/") == 2]
    assert len(checked_again) == paused["total"] - paused["cursor"]


def test_cancelling_keeps_what_was_found_and_removes_nobody(env):
    from sourcer import jobs

    db = env
    db["candidates"].insert_one({"_id": f"{ROLE}:old", "role": ROLE, "login": "old", "score": 1, "status": "new", "updated_at": __import__("datetime").datetime(2020, 1, 1)})
    job = start(db, remove_stale=True)
    status, _ = run(db, job, pause_after=3, on_pause=lambda: jobs.cancel(db, db["jobs"].find_one({})))
    assert status == "cancelled"
    assert db["jobs"].find_one({})["finished_at"] is not None
    assert "old" in logins(db) and logins(db) != ["old"]  # saved people stay; the stale one was not touched


def test_what_a_person_may_do_to_a_job_depends_on_its_state(env):
    from sourcer import jobs

    db = env
    job = jobs.create(db, ROLE, {}, "hr@acme.com")
    fresh = lambda: db["jobs"].find_one({})
    assert jobs.pause(db, fresh()) == "paused" and fresh()["status"] == "paused"
    jobs.resume(db, fresh())
    assert fresh()["status"] == "queued"
    assert jobs.cancel(db, fresh()) == "cancelled"
    for action in (jobs.pause, jobs.resume, jobs.cancel):
        with pytest.raises(jobs.JobError):
            action(db, fresh())  # a cancelled run is over
    del job


def test_a_role_can_have_only_one_active_run(env):
    from sourcer import jobs

    db = env
    jobs.create(db, ROLE, {}, "a")
    with pytest.raises(jobs.JobError):
        jobs.create(db, ROLE, {}, "b")
    jobs.cancel(db, db["jobs"].find_one({}))
    jobs.create(db, ROLE, {}, "b")  # fine once the first is over


# ---------- claiming ----------

def test_a_company_runs_one_job_at_a_time_and_claims_do_not_collide(env):
    from sourcer import jobs, roles_store

    db = env
    roles_store.import_role(db, {**ROLE_DATA, "name": "second-role"})
    jobs.create(db, ROLE, {}, "a")
    jobs.create(db, "second-role", {}, "a")
    first = jobs.claim_next(db, "w1")
    assert first["role"] == ROLE and first["status"] == "running"
    assert jobs.claim_next(db, "w2") is None  # the company's GitHub quota is taken, so the next waits
    jobs.finish(db, first["_id"], "done")
    assert jobs.claim_next(db, "w2")["role"] == "second-role"


def test_the_worker_loop_runs_the_first_waiting_job_and_skips_inactive_companies(mongo):
    from sourcer import jobs, roles_store, worker
    from sourcer.tenancy import registry
    from sourcer.tenancy.db import platform_db, tenant_db

    for slug in ("acme", "globex"):
        registry.create_tenant(slug, slug.title())
        roles_store.import_role(tenant_db(slug), ROLE_DATA)
        jobs.create(tenant_db(slug), ROLE, {}, "hr")
    platform_db()["tenants"].update_one({"slug": "acme"}, {"$set": {"status": "suspended"}})

    assert worker.run_once("w", client_factory=lambda token, **kw: FakeGitHub(universe(), **kw), crawl=site) is True
    assert tenant_db("globex")["jobs"].find_one({})["status"] == "done"
    from sourcer.tenancy.db import client, tenant_db_name
    assert client()[tenant_db_name("acme")]["jobs"].find_one({})["status"] == "queued"  # a suspended company's work is left alone
    assert worker.run_once("w", client_factory=lambda token, **kw: FakeGitHub(universe(), **kw), crawl=site) is False  # nothing left


def test_a_company_without_a_github_token_fails_the_job_with_a_clear_reason(mongo):
    from sourcer import jobs, roles_store, worker
    from sourcer.tenancy import registry
    from sourcer.tenancy.db import tenant_db

    registry.create_tenant("acme", "Acme")
    db = tenant_db("acme")
    roles_store.import_role(db, ROLE_DATA)
    jobs.create(db, ROLE, {}, "hr")
    assert worker.run_once("w") is True
    failed = db["jobs"].find_one({})
    assert failed["status"] == "failed" and "GitHub token" in failed["error"]


# ---------- failures ----------

def test_many_failures_in_a_row_stop_the_run_but_keep_progress(env, monkeypatch):
    from sourcer import pipeline

    monkeypatch.setattr(pipeline, "MAX_CONSECUTIVE_ERRORS", 3)
    db = env
    people = universe()
    for i in range(6):
        people[f"zz{i}"] = {**people["amy"], "login": f"zz{i}"}
    job = start(db, remove_stale=True)
    status, _ = run(db, job, people, fail={f"zz{i}" for i in range(6)})
    failed = db["jobs"].find_one({})
    assert status == "failed" and "in a row failed" in failed["error"]
    assert failed["cursor"] > 0 and logins(db)  # people before the failures were saved
    assert failed["removed"] == 0  # an incomplete run never removes anyone

    from sourcer import jobs

    jobs.resume(db, failed)  # a failed run can be tried again from where it stopped
    assert db["jobs"].find_one({})["status"] == "queued"


def test_a_role_deleted_mid_queue_fails_cleanly(env):
    db = env
    job = start(db)
    db["roles"].delete_many({})
    status, _ = run(db, job)
    assert status == "failed" and "no longer exists" in db["jobs"].find_one({})["error"]


def test_waiting_on_github_is_shown_to_the_user(env):
    db = env
    seen = []
    job = start(db)

    def factory(token, on_wait=None, **kw):
        def spy(reason, seconds):
            on_wait(reason, seconds)
            seen.append(db["jobs"].find_one({})["note"])
        return FakeGitHub(universe(), on_wait=spy, wait_at=2, **{k: v for k, v in kw.items() if k != "on_wait"})

    from sourcer import pipeline
    pipeline.run_job(db, job, "t", client_factory=factory, crawl=site)
    assert seen and "Waiting on GitHub (rate limit)" in seen[0] and "2 min" in seen[0]
    assert db["jobs"].find_one({})["note"] is None  # cleared once it carries on


# ---------- removing people who no longer match ----------

def test_a_clean_full_run_can_remove_people_who_no_longer_match_but_never_contacted_ones(env):
    db = env
    for login, status in (("gone", "new"), ("talked", "contacted")):
        db["candidates"].insert_one({"_id": f"{ROLE}:{login}", "role": ROLE, "login": login, "score": 5, "status": status, "updated_at": __import__("datetime").datetime(2020, 1, 1)})
    job = start(db, remove_stale=True)
    status, _ = run(db, job, {k: v for k, v in universe().items() if k != "flaky"})
    assert status == "done" and db["jobs"].find_one({})["removed"] == 1
    assert "gone" not in logins(db) and "talked" in logins(db)


@pytest.mark.parametrize("params,why", [({"remove_stale": True, "max_candidates": 3}, "capped"), ({"remove_stale": False}, "not asked for")])
def test_removal_only_happens_when_asked_for_and_not_on_a_capped_run(env, params, why):
    db = env
    db["candidates"].insert_one({"_id": f"{ROLE}:gone", "role": ROLE, "login": "gone", "score": 5, "status": "new", "updated_at": __import__("datetime").datetime(2020, 1, 1)})
    run(db, start(db, **params), {k: v for k, v in universe().items() if k != "flaky"})
    assert "gone" in logins(db), why


def test_a_run_with_errors_removes_nobody(env):
    db = env
    db["candidates"].insert_one({"_id": f"{ROLE}:gone", "role": ROLE, "login": "gone", "score": 5, "status": "new", "updated_at": __import__("datetime").datetime(2020, 1, 1)})
    run(db, start(db, remove_stale=True))  # flaky fails, so there is one error
    assert "gone" in logins(db) and db["jobs"].find_one({})["removed"] == 0
