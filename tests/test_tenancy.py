import pytest

KEY = "test-service-key"  # must match conftest.py

H = lambda tenant, user_id: {"X-Service-Key": KEY, "X-Tenant": tenant, "X-User-Id": user_id}


# ---------- tenants ----------

@pytest.mark.parametrize("slug", ["", "A", "acme_inc", "../etc", "a", "x" * 31, "-acme", "acme-", "ac me", "ac.me"])
def test_invalid_slugs_rejected(mongo, slug):
    from sourcer.tenancy import registry
    from sourcer.tenancy.db import TenantError

    with pytest.raises(TenantError):
        registry.create_tenant(slug, "x")


def test_duplicate_tenant_rejected(mongo):
    from sourcer.tenancy import registry
    from sourcer.tenancy.db import TenantError

    registry.create_tenant("acme", "Acme")
    with pytest.raises(TenantError):
        registry.create_tenant("acme", "Acme again")


def test_unknown_tenant_never_creates_a_database(mongo):
    from sourcer.tenancy.db import TenantError, client, tenant_db, tenant_db_name

    with pytest.raises(TenantError):
        tenant_db("typo-co")
    assert tenant_db_name("typo-co") not in client().list_database_names()


def test_inactive_tenant_blocked(mongo):
    from sourcer.tenancy import registry
    from sourcer.tenancy.db import TenantError, platform_db, tenant_db

    registry.create_tenant("acme", "Acme")
    platform_db()["tenants"].update_one({"slug": "acme"}, {"$set": {"status": "suspended"}})
    with pytest.raises(TenantError):
        tenant_db("acme")


def test_candidates_are_isolated_between_tenants(two_tenants):
    from sourcer.store import get_collection

    assert [d["login"] for d in get_collection("acme").find()] == ["acme-dev"]
    assert [d["login"] for d in get_collection("globex").find()] == ["globex-dev"]


def test_delete_tenant_removes_its_data_and_users_only(two_tenants):
    from sourcer.store import get_collection
    from sourcer.tenancy import registry
    from sourcer.tenancy.db import TenantError

    registry.delete_tenant("acme")
    with pytest.raises(TenantError):
        get_collection("acme")
    assert registry.list_users("acme") == []
    assert get_collection("globex").count_documents({}) == 1


# ---------- GitHub token ----------

def test_github_token_encrypted_at_rest(mongo):
    from sourcer.tenancy import registry
    from sourcer.tenancy.db import platform_db

    registry.create_tenant("acme", "Acme")
    registry.set_github_token("acme", "ghp_secretvalue123")
    raw = platform_db()["tenants"].find_one({"slug": "acme"})
    assert "ghp_secretvalue123" not in str(raw)
    assert registry.get_github_token("acme") == "ghp_secretvalue123"
    assert "github_token_enc" not in registry.list_tenants()[0]


def test_wrong_secrets_key_cannot_decrypt(mongo, monkeypatch):
    from sourcer.tenancy import crypto, registry

    registry.create_tenant("acme", "Acme")
    registry.set_github_token("acme", "ghp_x")
    monkeypatch.setenv("SECRETS_KEY", crypto.generate_key())
    with pytest.raises(RuntimeError):
        registry.get_github_token("acme")


# ---------- users and login ----------

def test_login_flow(two_tenants):
    from sourcer.tenancy import registry

    acme = two_tenants["acme"]
    user = registry.verify_login(acme["email"], acme["password"])
    assert user["tenant"] == "acme" and user["tenant_name"] == "Acme Inc" and "password_hash" not in user
    assert registry.verify_login(acme["email"].upper(), acme["password"])  # email is case-insensitive
    assert registry.verify_login(acme["email"], "wrong") is None
    assert registry.verify_login("nobody@x.com", "whatever") is None
    assert registry.verify_login("not-an-email", "x") is None


def test_password_never_stored_in_plaintext(two_tenants):
    from sourcer.tenancy.db import platform_db

    doc = platform_db()["users"].find_one({"email": two_tenants["acme"]["email"]})
    assert two_tenants["acme"]["password"] not in str(doc)
    assert doc["password_hash"].startswith("$argon2")


def test_lockout_after_repeated_failures(two_tenants):
    from sourcer.tenancy import registry

    acme = two_tenants["acme"]
    for _ in range(registry.MAX_FAILED_LOGINS):
        assert registry.verify_login(acme["email"], "wrong") is None
    # Now locked: even the right password is refused.
    assert registry.verify_login(acme["email"], acme["password"]) is None


def test_disabled_user_and_suspended_tenant_cannot_login(two_tenants):
    from sourcer.tenancy import registry
    from sourcer.tenancy.db import platform_db

    acme, globex = two_tenants["acme"], two_tenants["globex"]
    registry.set_disabled(acme["email"], True)
    assert registry.verify_login(acme["email"], acme["password"]) is None
    platform_db()["tenants"].update_one({"slug": "globex"}, {"$set": {"status": "suspended"}})
    assert registry.verify_login(globex["email"], globex["password"]) is None


def test_reset_and_change_password(two_tenants):
    from sourcer.tenancy import registry

    acme = two_tenants["acme"]
    new = registry.reset_password(acme["email"])
    assert registry.verify_login(acme["email"], acme["password"]) is None
    assert registry.verify_login(acme["email"], new)["must_change_password"] is True
    with pytest.raises(ValueError):
        registry.change_password(acme["email"], new, "short")
    assert registry.change_password(acme["email"], new, "a-much-better-password") is True
    assert registry.verify_login(acme["email"], "a-much-better-password")["must_change_password"] is False


def test_duplicate_and_invalid_users(two_tenants):
    from sourcer.tenancy import registry

    with pytest.raises(ValueError):
        registry.create_user("HR@ACME.com", "acme")  # same email, different case
    with pytest.raises(ValueError):
        registry.create_user("x@y.com", "acme", role="owner")
    with pytest.raises(Exception):
        registry.create_user("x@y.com", "no-such-tenant")


# ---------- API ----------

def test_api_requires_service_key(api, two_tenants):
    assert api.get("/health").status_code == 200
    h = H("acme", two_tenants["acme"]["user_id"])
    assert api.get("/me").status_code in (400, 401)
    assert api.get("/me", headers={**h, "X-Service-Key": "wrong"}).status_code == 401
    assert api.get("/me", headers=h).status_code == 200


def test_api_login(api, two_tenants):
    acme = two_tenants["acme"]
    ok = api.post("/auth/login", json={"email": acme["email"], "password": acme["password"]}, headers={"X-Service-Key": KEY})
    assert ok.status_code == 200 and ok.json()["tenant"] == "acme"
    assert acme["password"] not in ok.text and "password_hash" not in ok.text
    bad = api.post("/auth/login", json={"email": acme["email"], "password": "nope"}, headers={"X-Service-Key": KEY})
    assert bad.status_code == 401 and bad.json()["detail"] == "Invalid email or password"
    assert api.post("/auth/login", json={"email": acme["email"], "password": acme["password"]}).status_code == 401


def test_api_tenant_isolation(api, two_tenants):
    a, g = two_tenants["acme"], two_tenants["globex"]
    mine = api.get("/candidates", headers=H("acme", a["user_id"])).json()
    assert [c["login"] for c in mine["items"]] == ["acme-dev"] and mine["total"] == 1
    assert [c["login"] for c in api.get("/candidates", headers=H("globex", g["user_id"])).json()["items"]] == ["globex-dev"]
    # Acme's user presenting Globex as the tenant is refused, not served Globex's data.
    crossed = api.get("/candidates", headers=H("globex", a["user_id"]))
    assert crossed.status_code == 403 and "globex-dev" not in crossed.text


def test_api_rejects_bad_identity(api, two_tenants):
    a = two_tenants["acme"]
    assert api.get("/candidates", headers=H("acme", "not-an-id")).status_code == 403
    assert api.get("/candidates", headers=H("acme", "0" * 24)).status_code == 403
    assert api.get("/candidates", headers=H("nope", a["user_id"])).status_code == 403
    assert api.get("/candidates", headers={"X-Service-Key": KEY}).status_code == 400


# ---------- v0 migration ----------

def test_import_v0_copies_candidates_into_tenant(mongo):
    import argparse

    from sourcer import admin
    from sourcer.tenancy import registry
    from sourcer.tenancy.db import client, tenant_db

    registry.create_tenant("acme", "Acme")
    legacy = f"test_{mongo}_legacy"
    client()[legacy]["candidates"].insert_many([{"_id": "r:a", "login": "a", "score": 1}, {"_id": "r:b", "login": "b", "score": 2}])
    admin._tenant_import(argparse.Namespace(slug="acme", from_db=legacy))
    admin._tenant_import(argparse.Namespace(slug="acme", from_db=legacy))  # re-running is safe
    assert tenant_db("acme")["candidates"].count_documents({}) == 2
    assert client()[legacy]["candidates"].count_documents({}) == 2  # source untouched
