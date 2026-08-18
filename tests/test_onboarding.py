"""GET /onboarding/suggested-channels (app/routers/onboarding.py) — a thin
adapter over services/genre_artists.get_suggested_channels, monkeypatched
here so this makes no MusicBrainz/YouTube calls of its own."""

from app.routers import onboarding as onboarding_router


def test_suggested_channels_passes_through_parsed_genres(client, monkeypatch):
    seen = {}

    def fake_get_suggested_channels(db, genres):
        seen["genres"] = genres
        return [
            {
                "channel_id": "UC123",
                "title": "Some Artist",
                "thumbnail_url": None,
                "subscriber_count": 10,
                "channel_url": "https://www.youtube.com/channel/UC123",
            }
        ]

    monkeypatch.setattr(onboarding_router, "get_suggested_channels", fake_get_suggested_channels)

    res = client.get("/onboarding/suggested-channels?genres=Jazz, Lo-fi ,")

    assert res.status_code == 200
    assert seen["genres"] == ["Jazz", "Lo-fi"]  # trimmed, blanks dropped
    assert res.json() == [
        {
            "channel_id": "UC123",
            "title": "Some Artist",
            "thumbnail_url": None,
            "subscriber_count": 10,
            "channel_url": "https://www.youtube.com/channel/UC123",
        }
    ]


def test_suggested_channels_requires_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        res = anonymous.get("/onboarding/suggested-channels?genres=Jazz", follow_redirects=False)

    assert res.status_code == 303
    assert res.headers["location"] == "/login"
