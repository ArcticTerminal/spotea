"""The artist-refresh interval as per-user state (routers/settings.py).

It used to be a single AppSettings row shared by the *entire deployment*,
then per-Account across the profiles under it. With one login owning one
library it lives on the user row, and these pin that it is genuinely
per-user rather than shared again under a new name.
"""

from app.models import User


def test_default_interval_is_30_minutes(client):
    assert client.get("/settings").json()["refresh_interval_minutes"] == 30


def test_changing_the_interval_is_reflected_immediately(client):
    res = client.put("/settings", json={"refresh_interval_minutes": 120})
    assert res.json()["refresh_interval_minutes"] == 120
    assert client.get("/settings").json()["refresh_interval_minutes"] == 120


def test_an_invalid_interval_is_rejected(client):
    res = client.put("/settings", json={"refresh_interval_minutes": 7})
    assert res.status_code == 400


def test_the_interval_is_stored_on_the_user_row(client, db_session):
    client.put("/settings", json={"refresh_interval_minutes": 60})

    user = db_session.get(User, 1)
    db_session.refresh(user)
    assert user.refresh_interval_minutes == 60


def test_a_different_user_is_unaffected(client):
    """This must not have become a shared deployment-wide setting again
    under a different name."""
    client.put("/settings", json={"refresh_interval_minutes": 120})

    with client.__class__(client.app) as other:  # a second, unauthenticated TestClient
        other.post(
            "/register",
            data={
                "email": "second-user-settings@example.com",
                "password": "supersecret",
                "confirm_password": "supersecret",
            },
        )
        assert other.get("/settings").json()["refresh_interval_minutes"] == 30


def test_the_interval_resets_between_tests_1(client):
    """Paired with the test below: proves conftest.py's per-test reset of the
    preserved default user row actually works, not just that these two tests
    happen not to interfere by luck of file ordering."""
    assert client.get("/settings").json()["refresh_interval_minutes"] == 30
    client.put("/settings", json={"refresh_interval_minutes": 120})


def test_the_interval_resets_between_tests_2(client):
    assert client.get("/settings").json()["refresh_interval_minutes"] == 30
