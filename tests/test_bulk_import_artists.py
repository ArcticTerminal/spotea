"""Bulk import and the history scan, for a subscription list with musicians
in it.

An imported channel gets the same one-time scan a hand-added one does, and
that is right for a podcast — but since services/feed_add._as_artist_follow
an imported musician's feed is their "<Artist> - Topic" channel, which holds
their entire catalogue (1,064 uploads for Drake, measured). Scanning that
would turn "import my subscriptions" into "download everything those artists
have ever released", which nobody asked for. The rule matches the single-add
route's (see routers/feeds.py); these pin that the two agree.
"""

import app.services.bulk_import as bulk_import_module
from app.models import Feed
from app.youtube.urls import channel_feed_url

USER_ID = 1
TOPIC_ID = "UCDdTH-sn8qG64wK5ChFDQ4Q"
ARTIST_BROWSE_ID = "UC5ZkRnYd3__WBBGnAnWO9Cg"


def _run_one_line(monkeypatch, feed, scans):
    monkeypatch.setattr(bulk_import_module, "resolve_feed_url", lambda line: channel_feed_url(TOPIC_ID))
    monkeypatch.setattr(
        bulk_import_module,
        "create_feed_from_rss_url",
        lambda db, rss_url, user_id: (feed, 0, TOPIC_ID),
    )
    monkeypatch.setattr(
        bulk_import_module, "run_backfill", lambda feed_id, channel_id, db: scans.append(feed_id)
    )

    progress = {"total": 1, "resolved": 0, "done": 0, "results": []}
    bulk_import_module.run_bulk_import("job-artists", progress, ["https://a.example"], USER_ID)
    return progress


def test_an_imported_artist_is_not_scanned(monkeypatch):
    scans: list[int] = []
    feed = Feed(
        id=11,
        user_id=USER_ID,
        rss_url=channel_feed_url(TOPIC_ID),
        channel_title="Shirin David",
        artist_browse_id=ARTIST_BROWSE_ID,
    )

    progress = _run_one_line(monkeypatch, feed, scans)

    assert progress["results"][0]["status"] == "added"
    assert scans == []


def test_an_imported_channel_is_still_scanned(monkeypatch):
    """The other half of the rule — importing a subscription list is still
    how somebody moves their podcasts over, and those need their history."""
    scans: list[int] = []
    feed = Feed(
        id=12,
        user_id=USER_ID,
        rss_url=channel_feed_url(TOPIC_ID),
        channel_title="A Podcast",
    )

    _run_one_line(monkeypatch, feed, scans)

    assert scans == [12]
