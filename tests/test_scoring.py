from datetime import date

from sourcer.enrich import Candidate
from sourcer.role import Role
from sourcer.scoring import score

ROLE = Role(
    name="t", languages=["PHP"], keywords=["laravel"], locations=["India"],
    min_years=2.5, max_years=5, active_within_months=12,
)


def make(**overrides) -> Candidate:
    base = dict(
        login="a", name=None, url="u", location="India", bio=None, email=None, blog=None,
        twitter=None, hireable=None, followers=0, public_repos=10, est_years=3.0,
        last_active="2026-09-01", language_repos=5, keyword_repos=3,
    )
    return Candidate(**{**base, **overrides})


def test_ideal_candidate_scores_high_without_contact():
    result = score(make(), ROLE, today=date(2026, 10, 5))
    assert result["total"] == 90  # everything except the 10 contact points


def test_contact_points():
    assert score(make(email="a@b.c", hireable=True, blog="x"), ROLE, today=date(2026, 10, 5))["total"] == 100


def test_experience_fades_outside_range():
    inside = score(make(est_years=3), ROLE, today=date(2026, 10, 5))["experience"]
    close = score(make(est_years=6), ROLE, today=date(2026, 10, 5))["experience"]
    far = score(make(est_years=9), ROLE, today=date(2026, 10, 5))["experience"]
    assert inside > close > far == 0


def test_stale_account_loses_activity_points():
    result = score(make(last_active="2023-01-01"), ROLE, today=date(2026, 10, 5))
    assert result["activity"] == 0


def test_explain_lists_gates_and_rules():
    from sourcer.scoring import explain

    ROLE.min_language_share, ROLE.min_keyword_repos = 0.3, 1
    result = explain(make(language_share=0.5, matching_repos=["shop"]), ROLE, today=date(2026, 10, 5))
    assert result["total"] == 90
    assert all(g["ok"] for g in result["gates"])
    assert {r["name"] for r in result["rules"]} >= {"Framework evidence", "Experience fit", "Recent activity"}
    assert result["rules"][0]["level"] == "full"
    failing = explain(make(language_share=0.1), ROLE, today=date(2026, 10, 5))["gates"]
    assert [g["ok"] for g in failing] == [True, False, True]


def test_location_excludes_other_countries():
    from sourcer.enrich import location_ok

    role = Role(name="t", languages=["PHP"], keywords=[], locations=["Hyderabad", "India"], exclude_locations=["Pakistan"])
    assert location_ok("Hyderabad, India", role)
    assert not location_ok("Hyderabad, Pakistan", role)
    assert not location_ok(None, role)
