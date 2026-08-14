from fastapi.testclient import TestClient

from app.main import app

# Must match conftest.py's own DEFAULT_ACCOUNT_EMAIL/DEFAULT_ACCOUNT_PASSWORD
# (the bootstrap account _init_schema seeds) — duplicated rather than
# imported since tests/ isn't a real package (no __init__.py) and importing
# conftest as `tests.conftest` re-executes its module-level setup as a
# second, different module instance.
DEFAULT_ACCOUNT_EMAIL = "test@example.com"
DEFAULT_ACCOUNT_PASSWORD = "test-password"


def test_protected_route_redirects_to_login_when_unauthenticated():
    with TestClient(app) as anon:
        res = anon.get("/content", follow_redirects=False)

    assert res.status_code == 303
    assert res.headers["location"] == "/login"


def test_login_with_wrong_password_is_rejected():
    with TestClient(app) as anon:
        res = anon.post(
            "/login", data={"email": DEFAULT_ACCOUNT_EMAIL, "password": "definitely-not-it"}
        )

    assert res.status_code == 401


def test_login_with_unknown_email_is_rejected():
    with TestClient(app) as anon:
        res = anon.post("/login", data={"email": "nobody@example.com", "password": "whatever123"})

    assert res.status_code == 401


def test_login_with_correct_password_grants_access(client):
    # `client` (from conftest) has already logged in for real — confirm a
    # protected route is actually reachable now, not just that login itself
    # returned 200.
    res = client.get("/content")
    assert res.status_code == 200


def test_logout_revokes_access():
    with TestClient(app) as c:
        c.post("/login", data={"email": DEFAULT_ACCOUNT_EMAIL, "password": DEFAULT_ACCOUNT_PASSWORD})
        assert c.get("/content").status_code == 200

        c.post("/logout")
        res = c.get("/content", follow_redirects=False)

    assert res.status_code == 303
    assert res.headers["location"] == "/login"


def test_register_creates_account_and_logs_in():
    with TestClient(app) as anon:
        res = anon.post(
            "/register",
            data={
                "email": "newuser@example.com",
                "password": "supersecret",
                "confirm_password": "supersecret",
            },
            follow_redirects=False,
        )
        assert res.status_code == 303
        assert res.headers["location"] == "/#home"
        assert anon.get("/content").status_code == 200


def test_register_rejects_duplicate_email():
    # DEFAULT_ACCOUNT_EMAIL is the bootstrap account seeded in conftest,
    # always present.
    with TestClient(app) as anon:
        res = anon.post(
            "/register",
            data={
                "email": DEFAULT_ACCOUNT_EMAIL,
                "password": "supersecret",
                "confirm_password": "supersecret",
            },
        )

    assert res.status_code == 400
    assert "already registered" in res.text.lower()


def test_register_rejects_mismatched_passwords():
    with TestClient(app) as anon:
        res = anon.post(
            "/register",
            data={
                "email": "another@example.com",
                "password": "supersecret",
                "confirm_password": "different123",
            },
        )

    assert res.status_code == 400


def test_register_rejects_short_password():
    with TestClient(app) as anon:
        res = anon.post(
            "/register",
            data={"email": "shortpw@example.com", "password": "short1", "confirm_password": "short1"},
        )

    assert res.status_code == 400


def test_relogin_returns_to_the_profile_active_before_logout():
    with TestClient(app) as c:
        c.post("/login", data={"email": DEFAULT_ACCOUNT_EMAIL, "password": DEFAULT_ACCOUNT_PASSWORD})

        # Create (which auto-switches into) a second profile, so the active
        # one is no longer the account's first/default.
        other_profile_id = c.post("/profiles", json={"name": "Kids"}).json()["id"]
        assert c.get("/profiles").json() == [
            {"id": 1, "name": "Default", "is_current": False},
            {"id": other_profile_id, "name": "Kids", "is_current": True},
        ]

        c.post("/logout")
        c.post("/login", data={"email": DEFAULT_ACCOUNT_EMAIL, "password": DEFAULT_ACCOUNT_PASSWORD})

        # Logout clears the whole session — without account.last_active_profile_id
        # persisting it, this would silently fall back to profile id 1 instead.
        profiles = c.get("/profiles").json()
        current = next(p for p in profiles if p["is_current"])
        assert current["id"] == other_profile_id

        # Clean up so this doesn't linger for other tests sharing the
        # bootstrap account (matches this file's other tests, which never
        # otherwise create extra profiles on the shared account).
        c.post("/profiles/1/switch")
        c.delete(f"/profiles/{other_profile_id}")
