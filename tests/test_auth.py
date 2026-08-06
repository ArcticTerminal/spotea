from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_protected_route_redirects_to_login_when_unauthenticated():
    with TestClient(app) as anon:
        res = anon.get("/content", follow_redirects=False)

    assert res.status_code == 303
    assert res.headers["location"] == "/login"


def test_login_with_wrong_password_is_rejected():
    with TestClient(app) as anon:
        res = anon.post("/login", data={"password": "definitely-not-it"})

    assert res.status_code == 401


def test_login_with_correct_password_grants_access(client):
    # `client` (from conftest) has already logged in for real — confirm a
    # protected route is actually reachable now, not just that login itself
    # returned 200.
    res = client.get("/content")
    assert res.status_code == 200


def test_logout_revokes_access():
    with TestClient(app) as c:
        c.post("/login", data={"password": settings.app_password})
        assert c.get("/content").status_code == 200

        c.post("/logout")
        res = c.get("/content", follow_redirects=False)

    assert res.status_code == 303
    assert res.headers["location"] == "/login"
