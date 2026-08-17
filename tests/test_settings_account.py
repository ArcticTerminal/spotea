"""The feed-refresh interval as Account state, not per-profile (routers/settings.py).

Locked decision: one account's choice covers every profile under it; a
different account picks independently. Before this it was a single AppSettings
row shared by the *entire deployment*, which is the bug these tests replace —
see [[project-audit-decisions-2026-08-17]].
"""

from app.models import Account


def test_default_interval_is_30_minutes(client):
    assert client.get("/settings").json()["feed_refresh_interval_minutes"] == 30


def test_changing_the_interval_is_reflected_immediately(client):
    res = client.put("/settings", json={"feed_refresh_interval_minutes": 120})
    assert res.json()["feed_refresh_interval_minutes"] == 120
    assert client.get("/settings").json()["feed_refresh_interval_minutes"] == 120


def test_an_invalid_interval_is_rejected(client):
    res = client.put("/settings", json={"feed_refresh_interval_minutes": 7})
    assert res.status_code == 400


def test_the_interval_is_stored_on_the_account_not_the_profile(client, db_session):
    client.put("/settings", json={"feed_refresh_interval_minutes": 60})

    account = db_session.get(Account, 1)
    db_session.refresh(account)
    assert account.feed_refresh_interval_minutes == 60


def test_every_profile_under_one_account_shares_its_interval(client):
    """The locked decision, end to end: a second profile under the same
    login sees the interval the first profile picked, without setting it
    again — because it lives on the shared Account, not on either User row."""
    client.put("/settings", json={"feed_refresh_interval_minutes": 15})

    other_id = client.post("/profiles", json={"name": "Kids"}).json()["id"]
    client.post(f"/profiles/{other_id}/switch")

    assert client.get("/settings").json()["feed_refresh_interval_minutes"] == 15

    # Clean up so this doesn't linger for other tests sharing the bootstrap
    # account, matching this file's other tests which never otherwise create
    # extra profiles.
    client.post("/profiles/1/switch")
    client.delete(f"/profiles/{other_id}")


def test_a_different_account_is_unaffected_by_another_accounts_interval(client):
    """The other half of the locked decision: this must not have become a
    new shared-deployment-wide setting under a different name."""
    client.put("/settings", json={"feed_refresh_interval_minutes": 120})

    with client.__class__(client.app) as other:  # a second, unauthenticated TestClient
        other.post(
            "/register",
            data={
                "email": "second-account-settings@example.com",
                "password": "supersecret",
                "confirm_password": "supersecret",
            },
        )
        assert other.get("/settings").json()["feed_refresh_interval_minutes"] == 30


def test_the_interval_resets_between_tests_1(client):
    """Paired with the test below: proves conftest.py's per-test reset of the
    preserved default account row actually works, not just that these two
    tests happen not to interfere by luck of file ordering."""
    assert client.get("/settings").json()["feed_refresh_interval_minutes"] == 30
    client.put("/settings", json={"feed_refresh_interval_minutes": 120})


def test_the_interval_resets_between_tests_2(client):
    assert client.get("/settings").json()["feed_refresh_interval_minutes"] == 30
