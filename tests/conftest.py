import os
import uuid

import pytest
from fastapi.testclient import TestClient

KEY = "test-service-key"


@pytest.fixture
def mongo(monkeypatch):
    """Isolated platform + tenant databases on the local MongoDB, dropped afterwards."""
    from sourcer.tenancy import crypto
    from sourcer.tenancy.db import client, reset_client

    run = uuid.uuid4().hex[:8]
    monkeypatch.setenv("MONGODB_URI", os.getenv("TEST_MONGODB_URI", "mongodb://localhost:27017"))
    monkeypatch.setenv("PLATFORM_DB", f"test_{run}_platform")
    monkeypatch.setenv("TENANT_DB_PREFIX", f"test_{run}_t_")
    monkeypatch.setenv("SECRETS_KEY", crypto.generate_key())
    monkeypatch.setenv("API_SERVICE_KEY", KEY)
    reset_client()
    try:
        client().admin.command("ping")
    except Exception:
        pytest.skip("MongoDB is not running")
    yield run
    for name in client().list_database_names():
        if name.startswith(f"test_{run}_"):
            client().drop_database(name)
    reset_client()


def settled(user):
    """Clear the one-time-password flag, as a user who already chose their own password."""
    from sourcer.tenancy.db import platform_db

    platform_db()["users"].update_one({"_id": user["_id"]}, {"$set": {"must_change_password": False}})
    return user


@pytest.fixture
def api(mongo):
    from sourcer.api.app import app

    return TestClient(app)


@pytest.fixture
def two_tenants(mongo):
    """Tenants `acme` and `globex`, each with one admin and one candidate."""
    from sourcer.tenancy import registry
    from sourcer.tenancy.db import tenant_db

    out = {}
    for slug in ("acme", "globex"):
        registry.create_tenant(slug, slug.title() + " Inc")
        user, password = registry.create_user(f"hr@{slug}.com", slug, "admin")
        settled(user)
        tenant_db(slug)["candidates"].insert_one(
            {"_id": f"r:{slug}-dev", "login": f"{slug}-dev", "role": "r", "score": 80, "status": "new"}
        )
        out[slug] = {"user_id": str(user["_id"]), "email": user["email"], "password": password}
    return out
