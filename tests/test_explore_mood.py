"""A mood's playlists (GET /partials/detail/yt-mood/{params}), opened from
Explore's "Moods & genres" row — see templates/_mood_panel.html.

Both YouTube Music calls are monkeypatched out, same as
test_explore_remote.py. What's under test: the params validation guarding
the route, that a title passed in is used as-is, and that a missing one
falls back to one extra category lookup rather than 500ing or guessing.
"""

import pytest

from app.youtube.models import PlaylistSearchResult
from app.youtube.music import MoodCategory

PARAMS = "ggMPOg1uX1JOQWZFeDByc2Jm"


def _playlist(playlist_id, title="A Playlist"):
    return PlaylistSearchResult(
        playlist_id=playlist_id, title=title, thumbnail_url=None, channel_title="YouTube Music"
    )


@pytest.fixture
def fake_mood(monkeypatch):
    """Installs a mood's playlists, and records what params it was asked
    for so tests can assert nothing extra was fetched."""
    requested = []

    def fetch(params):
        requested.append(params)
        return [_playlist("aaaaaaaaaaaaaaaaaaaaaaa"), _playlist("bbbbbbbbbbbbbbbbbbbbbbb")]

    monkeypatch.setattr("app.services.remote_detail.fetch_mood_playlists", fetch)

    def explode():
        raise AssertionError("a title provided by the caller must not trigger a category lookup")

    monkeypatch.setattr("app.services.remote_detail.fetch_mood_categories", explode)
    return requested


def test_a_mood_panel_renders_its_playlists(client, fake_mood):
    res = client.get(f"/partials/detail/yt-mood/{PARAMS}", params={"title": "Sad"})

    assert res.status_code == 200
    assert "Sad" in res.text
    assert 'data-playlist-id="aaaaaaaaaaaaaaaaaaaaaaa"' in res.text
    assert fake_mood == [PARAMS]


def test_a_mood_panel_has_no_hero_or_play_all(client, fake_mood):
    """No single list of tracks exists here to play — see
    _mood_panel.html's own docstring on why it skips _detail_hero.html."""
    text = client.get(f"/partials/detail/yt-mood/{PARAMS}", params={"title": "Sad"}).text

    assert "detail-play-all" not in text
    assert "channel-hero-avatar" not in text


@pytest.mark.parametrize("params", ["has spaces!", "a/b", "x" * 33])
def test_a_non_params_string_is_rejected_without_being_fetched(client, fake_mood, params):
    assert client.get(f"/partials/detail/yt-mood/{params}", params={"title": "Sad"}).status_code == 404
    assert fake_mood == []


def test_an_empty_playlist_list_is_a_404(client, monkeypatch):
    monkeypatch.setattr("app.services.remote_detail.fetch_mood_playlists", lambda params: [])
    monkeypatch.setattr(
        "app.services.remote_detail.fetch_mood_categories",
        lambda: [MoodCategory(title="Sad", params=PARAMS, section="Moods & moments")],
    )

    res = client.get(f"/partials/detail/yt-mood/{PARAMS}", params={"title": "Sad"})

    assert res.status_code == 404


def test_a_missing_title_falls_back_to_a_category_lookup(client, monkeypatch):
    """A reload or a shared link arrives with no title — the only case this
    extra request is worth paying for."""
    monkeypatch.setattr(
        "app.services.remote_detail.fetch_mood_playlists", lambda params: [_playlist("ccccccccccccccccccccccc")]
    )
    lookups = []

    def fetch_categories():
        lookups.append(1)
        return [
            MoodCategory(title="Other", params="zzzzzzzzzzzzzzzzzzzzzzzz", section="Moods & moments"),
            MoodCategory(title="Sad", params=PARAMS, section="Moods & moments"),
        ]

    monkeypatch.setattr("app.services.remote_detail.fetch_mood_categories", fetch_categories)

    res = client.get(f"/partials/detail/yt-mood/{PARAMS}")

    assert res.status_code == 200
    assert "Sad" in res.text
    assert lookups == [1]


def test_a_params_matching_no_known_category_is_a_404(client, monkeypatch):
    monkeypatch.setattr("app.services.remote_detail.fetch_mood_playlists", lambda params: [_playlist("x")])
    monkeypatch.setattr("app.services.remote_detail.fetch_mood_categories", lambda: [])

    res = client.get(f"/partials/detail/yt-mood/{PARAMS}")

    assert res.status_code == 404


def test_yt_mood_route_requires_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        res = anonymous.get(f"/partials/detail/yt-mood/{PARAMS}", follow_redirects=False)

    assert res.status_code == 303
