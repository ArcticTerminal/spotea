import os

from app.auth import hash_password
from app.content_query import query_content_page
from app.models import Account, Content, Feed, User
from app.storage import unlink_if_unshared

# Must match conftest.py's own DEFAULT_ACCOUNT_ID/DEFAULT_PROFILE_ID (the
# bootstrap account/profile _init_schema seeds) — duplicated rather than
# imported since tests/ isn't a real package (no __init__.py) and importing
# conftest as `tests.conftest` re-executes its module-level setup as a
# second, different module instance.
DEFAULT_ACCOUNT_ID = 1
DEFAULT_PROFILE_ID = 1


def _seed_content(db_session, user_id: int, video_id: str = "vid00000001") -> Content:
    feed = Feed(user_id=user_id, rss_url=f"https://example.com/{user_id}", channel_title="Test Channel")
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    content = Content(feed_id=feed.id, user_id=user_id, video_id=video_id, title="A video")
    db_session.add(content)
    db_session.commit()
    return content


def test_new_profile_starts_with_an_empty_library(client, db_session):
    _seed_content(db_session, DEFAULT_PROFILE_ID)

    res = client.post("/profiles", json={"name": "Music"})
    assert res.status_code == 201
    new_profile_id = res.json()["id"]
    assert res.json()["is_current"] is True

    # Creating a profile auto-switches into it — the default profile's
    # content must not leak into the brand-new one.
    items, _page, _total_pages = query_content_page(db_session, new_profile_id)
    assert items == []


def test_switching_back_restores_the_original_profile_scope(client, db_session):
    _seed_content(db_session, DEFAULT_PROFILE_ID)

    new_profile_id = client.post("/profiles", json={"name": "Music"}).json()["id"]
    items, _page, _total_pages = query_content_page(db_session, new_profile_id)
    assert items == []

    res = client.post(f"/profiles/{DEFAULT_PROFILE_ID}/switch")
    assert res.status_code == 200
    assert res.json()["is_current"] is True

    items, _page, _total_pages = query_content_page(db_session, DEFAULT_PROFILE_ID)
    assert len(items) == 1

    # The other profile's (empty) scope is still intact and independently reachable.
    client.post(f"/profiles/{new_profile_id}/switch")
    items, _page, _total_pages = query_content_page(db_session, new_profile_id)
    assert items == []


def test_cannot_delete_the_last_remaining_profile(client):
    res = client.delete(f"/profiles/{DEFAULT_PROFILE_ID}")
    assert res.status_code == 409


def test_deleting_a_non_active_profile_leaves_the_active_one_untouched(client, db_session):
    new_profile_id = client.post("/profiles", json={"name": "Music"}).json()["id"]
    client.post(f"/profiles/{DEFAULT_PROFILE_ID}/switch")

    res = client.delete(f"/profiles/{new_profile_id}")
    assert res.status_code == 204

    profiles = client.get("/profiles").json()
    assert [p["id"] for p in profiles] == [DEFAULT_PROFILE_ID]
    assert profiles[0]["is_current"] is True


def test_deleting_a_profile_takes_its_feeds_and_content_with_it(client, db_session):
    """The rows go through one bulk DELETE per table now, not the ORM cascade
    loading every child row into the session to delete them one at a time
    (1.7 of the 2.3 seconds a real 28,866-row profile took). What has to stay
    true either way: the deleted profile keeps nothing, and the surviving one
    loses nothing."""
    doomed_id = client.post("/profiles", json={"name": "Doomed"}).json()["id"]
    _seed_content(db_session, doomed_id, video_id="viddoomed01")
    kept = _seed_content(db_session, DEFAULT_PROFILE_ID, video_id="vidkept0001")
    client.post(f"/profiles/{DEFAULT_PROFILE_ID}/switch")

    assert client.delete(f"/profiles/{doomed_id}").status_code == 204

    assert db_session.query(Content).filter(Content.user_id == doomed_id).count() == 0
    assert db_session.query(Feed).filter(Feed.user_id == doomed_id).count() == 0
    assert db_session.get(User, doomed_id) is None
    assert db_session.query(Content).filter(Content.id == kept.id).count() == 1


def test_unlink_if_unshared_keeps_file_referenced_by_another_profile(db_session, tmp_path):
    shared_path = str(tmp_path / "shared.m4a")
    open(shared_path, "w").close()

    other_profile = User(name="Music", account_id=DEFAULT_ACCOUNT_ID)
    db_session.add(other_profile)
    db_session.commit()
    db_session.refresh(other_profile)

    content_a = _seed_content(db_session, DEFAULT_PROFILE_ID, video_id="vidshared01")
    content_a.status = "ready"
    content_a.file_path = shared_path

    content_b = _seed_content(db_session, other_profile.id, video_id="vidshared01")
    content_b.status = "ready"
    content_b.file_path = shared_path
    db_session.commit()

    # Deleting profile A's copy must not remove the file profile B still needs.
    unlink_if_unshared(db_session, shared_path, content_a.id)
    assert os.path.exists(shared_path)
    # Real callers (clear_all/delete_content/...) always flip the row's status
    # right after calling this, so it stops counting as a live reference.
    content_a.status = "not_downloaded"
    db_session.commit()

    # Only once the last referencing row is gone does the file actually go.
    unlink_if_unshared(db_session, shared_path, content_b.id)
    assert not os.path.exists(shared_path)


def test_profiles_are_isolated_between_accounts(client, db_session):
    other_account = Account(email="other@example.com", password_hash=hash_password("irrelevant1"))
    db_session.add(other_account)
    db_session.flush()
    other_profile = User(name="Other Household", account_id=other_account.id)
    db_session.add(other_profile)
    db_session.commit()

    # Another account's profile must never appear in this account's list.
    profiles = client.get("/profiles").json()
    assert [p["id"] for p in profiles] == [DEFAULT_PROFILE_ID]

    # Nor be switchable/editable/deletable through this account's session —
    # 404, not 403, so existence of the other account's profile id isn't leaked.
    assert client.post(f"/profiles/{other_profile.id}/switch").status_code == 404
    assert client.put(f"/profiles/{other_profile.id}", json={"name": "Hijacked"}).status_code == 404
    assert client.delete(f"/profiles/{other_profile.id}").status_code == 404

    # This account's own last-profile guard must be unaffected by the other
    # account's profile existing (regression test for the count being scoped
    # by account_id, not counted globally across every account).
    assert client.delete(f"/profiles/{DEFAULT_PROFILE_ID}").status_code == 409
