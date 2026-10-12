"""Module access, one-time-password enforcement, session versions and login throttling."""

import pytest

from tests.conftest import KEY, settled

PASSWORD = "a-long-enough-password"


def headers(tenant, user, version=None):
    h = {"X-Service-Key": KEY, "X-Tenant": tenant, "X-User-Id": str(user["_id"])}
    if version is not None:
        h["X-Session-Version"] = str(version)
    return h


@pytest.fixture
def acme(two_tenants):
    from sourcer.tenancy import registry

    out = {}
    for role in ("recruiter", "employee"):
        user, first = registry.create_user(f"{role}@acme.com", "acme", role)
        registry.change_password(user["email"], first, PASSWORD)
        out[role] = registry.get_user_by_id(user["_id"])
    return out


def test_modules_by_role(api, acme, two_tenants):
    from sourcer.tenancy import registry

    admin = registry.get_user_by_id(two_tenants["acme"]["user_id"])
    got = lambda u: api.get("/me", headers=headers("acme", u)).json()["modules"]  # noqa: E731
    assert got(admin) == ["hiring", "people"]
    assert got(acme["recruiter"]) == ["hiring"]
    assert got(acme["employee"]) == ["people"]


def test_employee_is_refused_on_every_hiring_route(api, acme):
    """Walks the real route table, so a hiring route added later is covered without a new test."""
    open_paths = ("/health", "/auth/", "/me", "/public/")
    checked = 0
    for path, methods in api.app.openapi()["paths"].items():
        if path.startswith(open_paths):
            continue
        for method in methods:
            url = path.replace("{", "").replace("}", "")  # any value will do: the module check runs first
            res = api.request(method.upper(), url, headers=headers("acme", acme["employee"]), json={})
            assert res.status_code == 403, f"{method.upper()} {path} let an employee in ({res.status_code})"
            assert "x-session" not in res.headers
            checked += 1
    assert checked > 20  # the walk really found the hiring routes


def test_recruiter_still_reaches_hiring(api, acme):
    assert api.get("/roles", headers=headers("acme", acme["recruiter"])).status_code == 200


def test_explicit_modules_override_the_role_default(api, acme):
    from sourcer.tenancy.db import platform_db

    platform_db()["users"].update_one({"_id": acme["recruiter"]["_id"]}, {"$set": {"modules": []}})
    assert api.get("/roles", headers=headers("acme", acme["recruiter"])).status_code == 403


def test_one_time_password_blocks_everything_but_the_change(api, mongo):
    from sourcer.tenancy import registry

    registry.create_tenant("acme", "Acme")
    user, first = registry.create_user("new@acme.com", "acme", "admin")
    h = headers("acme", user)
    assert api.get("/me", headers=h).json()["must_change_password"] is True
    blocked = api.get("/roles", headers=h)
    assert blocked.status_code == 403 and "x-session" not in blocked.headers
    assert api.post("/me/password", json={"old": first, "new": PASSWORD}, headers=h).status_code == 200
    assert api.get("/roles", headers=h).status_code == 200


def test_session_version_ends_old_sessions(api, mongo):
    from sourcer.tenancy import registry

    registry.create_tenant("acme", "Acme")
    user, first = registry.create_user("a@acme.com", "acme", "admin")
    me = api.get("/me", headers=headers("acme", user)).json()
    assert api.get("/me", headers=headers("acme", user, me["session_version"])).status_code == 200

    new = api.post("/me/password", json={"old": first, "new": PASSWORD}, headers=headers("acme", user, me["session_version"])).json()
    stale = api.get("/me", headers=headers("acme", user, me["session_version"]))
    assert stale.status_code == 401 and stale.headers["x-session"] == "ended"  # a copied cookie is dead
    assert api.get("/me", headers=headers("acme", user, new["session_version"])).status_code == 200  # the caller keeps going

    registry.reset_password("a@acme.com")  # HR resets: every session ends
    assert api.get("/me", headers=headers("acme", user, new["session_version"])).status_code == 401
    registry.set_disabled("a@acme.com", True)
    assert api.get("/me", headers=headers("acme", user)).status_code == 401


def test_login_is_throttled_per_ip_and_recorded(api, two_tenants):
    from sourcer.tenancy.db import platform_db

    body = {"email": "nobody@acme.com", "password": "x"}
    ip = {"X-Service-Key": KEY, "X-Client-IP": "203.0.113.9"}
    codes = [api.post("/auth/login", json=body, headers=ip).status_code for _ in range(32)]
    assert codes[:30] == [401] * 30 and codes[30:] == [429, 429]
    # another visitor is not affected
    assert api.post("/auth/login", json=body, headers={**ip, "X-Client-IP": "198.51.100.1"}).status_code == 401
    kinds = {e["kind"] for e in platform_db()["auth_events"].find()}
    assert {"login_failed", "login_throttled"} <= kinds
    # real sign-ins are never counted against the limit, so a shared address (an office) keeps working
    good = {"email": two_tenants["acme"]["email"], "password": two_tenants["acme"]["password"]}
    office = {**ip, "X-Client-IP": "192.0.2.50"}
    assert [api.post("/auth/login", json=good, headers=office).status_code for _ in range(35)] == [200] * 35
    assert "password" not in str(list(platform_db()["auth_events"].find()))


def test_login_rejects_oversized_or_unknown_fields(api, two_tenants):
    h = {"X-Service-Key": KEY}
    assert api.post("/auth/login", json={"email": "a@b.co", "password": "x" * 300}, headers=h).status_code == 422
    assert api.post("/auth/login", json={"email": "a@b.co", "password": "x", "extra": 1}, headers=h).status_code == 422


@pytest.mark.parametrize("bad", ["", "a", "a@b", "@b.co", "a b@c.co", "a@b@c.co", "x" * 250 + "@b.co"])
def test_email_validation(mongo, bad):
    from sourcer.tenancy import registry

    registry.create_tenant("acme", "Acme")
    with pytest.raises(ValueError):
        registry.create_user(bad, "acme", "admin")


def test_setup_creates_company_and_admin(mongo, capsys):
    from sourcer import admin
    from sourcer.tenancy import registry

    admin.main(["setup", "--company", "Acme Pvt. Ltd", "--email", "Boss@Acme.com"])
    out = capsys.readouterr().out
    assert "acme-pvt-ltd" in out and "One-time password" in out
    user = registry.list_users("acme-pvt-ltd")[0]
    assert user["email"] == "boss@acme.com" and user["role"] == "admin" and user["must_change_password"] is True
    with pytest.raises(SystemExit):  # running it twice does not make a second company
        admin.main(["setup", "--company", "Acme Pvt. Ltd", "--email", "other@acme.com"])


def test_setup_slug_is_valid_for_odd_names():
    from sourcer.admin import slugify
    from sourcer.tenancy.db import validate_slug

    for name in ("Acme", "  Zibtek Software!! ", "A" * 80, "Café 24/7"):
        validate_slug(slugify(name))


def test_indexes_ensure_is_repeatable(two_tenants):
    from sourcer import admin
    from sourcer.tenancy.db import tenant_db

    admin.main(["indexes", "ensure", "--all"])
    admin.main(["indexes", "ensure", "--all"])
    names = {i["name"] for i in tenant_db("acme")["audit_log"].list_indexes()}
    assert any(n.startswith("at_") for n in names)
