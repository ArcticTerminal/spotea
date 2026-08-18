"""Onboarding's genre-artist cache (app/services/genre_artists.py).

Every MusicBrainz and YouTube call is monkeypatched out — real network here
would make the suite slow, flaky, and (for the YouTube half) a genuine
rate-limiting liability on a residential IP, same reasoning as
test_recommendations.py's fake_search fixture.
"""

from app.models import GenreArtist
from app.services import genre_artists as ga
from app.timeutil import utcnow
from app.youtube.search import ChannelUploads

GENRE = "Jazz"


def _row(db_session, *, channel_id, resolved=False, **extra):
    row = GenreArtist(
        genre=GENRE,
        artist_name=extra.pop("artist_name", f"Artist {channel_id}"),
        channel_id=channel_id,
        channel_url=f"https://www.youtube.com/channel/{channel_id}",
        resolved_at=utcnow() if resolved else None,
        **extra,
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_musicbrainz_youtube_channel_extracts_a_plain_channel_url(monkeypatch):
    monkeypatch.setattr(
        ga,
        "_mb_get",
        lambda path, params: {
            "relations": [
                {"type": "allmusic", "url": {"resource": "https://www.allmusic.com/artist/x"}},
                {
                    "type": "youtube",
                    "url": {"resource": "https://www.youtube.com/channel/UCabc123DEF456ghijk"},
                },
            ]
        },
    )

    result = ga._musicbrainz_youtube_channel("some-mbid")

    assert result == (
        "UCabc123DEF456ghijk",
        "https://www.youtube.com/channel/UCabc123DEF456ghijk",
    )


def test_musicbrainz_youtube_channel_skips_a_handle_link(monkeypatch):
    """A @handle or /c/Name youtube relation can't be turned into a
    channel_id for free — skipped rather than guessed at."""
    monkeypatch.setattr(
        ga,
        "_mb_get",
        lambda path, params: {
            "relations": [{"type": "youtube", "url": {"resource": "https://www.youtube.com/@someartist"}}]
        },
    )

    assert ga._musicbrainz_youtube_channel("some-mbid") is None


def test_musicbrainz_youtube_channel_none_without_a_youtube_relation(monkeypatch):
    monkeypatch.setattr(ga, "_mb_get", lambda path, params: {"relations": []})

    assert ga._musicbrainz_youtube_channel("some-mbid") is None


def test_seed_genre_inserts_up_to_the_target_and_skips_duplicates(db_session, monkeypatch):
    # Only even-indexed candidates resolve to a channel below, so this needs
    # more than 2 * ARTISTS_PER_GENRE candidates to fill the target.
    candidates = [{"id": f"mbid-{i}", "name": f"Artist {i}"} for i in range(ga.ARTISTS_PER_GENRE * 2 + 6)]
    monkeypatch.setattr(ga, "_musicbrainz_artist_candidates", lambda genre, limit: candidates)

    # Every other candidate has no usable youtube link, so seed_genre has to
    # walk past more than ARTISTS_PER_GENRE candidates to fill the target —
    # exercises the "keep going until enough, not just take the first N".
    def fake_channel(mbid):
        i = int(mbid.split("-")[1])
        if i % 2:
            return None
        return f"UC{i:018d}", f"https://www.youtube.com/channel/UC{i:018d}"

    monkeypatch.setattr(ga, "_musicbrainz_youtube_channel", fake_channel)

    added = ga.seed_genre(GENRE, db_session)
    db_session.commit()

    assert added == ga.ARTISTS_PER_GENRE
    rows = db_session.query(GenreArtist).filter(GenreArtist.genre == GENRE).all()
    assert len(rows) == ga.ARTISTS_PER_GENRE
    assert all(row.resolved_at is None for row in rows)


def test_seed_genre_is_a_noop_once_the_target_is_already_met(db_session, monkeypatch):
    for i in range(ga.ARTISTS_PER_GENRE):
        _row(db_session, channel_id=f"UC{i:018d}")

    called = []
    monkeypatch.setattr(
        ga, "_musicbrainz_artist_candidates", lambda genre, limit: called.append(1) or []
    )

    added = ga.seed_genre(GENRE, db_session)

    assert added == 0
    assert called == []  # never even asked MusicBrainz — already had enough


def test_get_suggested_channels_resolves_unresolved_rows_once(db_session, monkeypatch):
    unresolved = _row(db_session, channel_id="UCunresolved00000000")
    _row(
        db_session,
        channel_id="UCresolved000000000",
        resolved=True,
        title="Cached Title",
        thumbnail_url="/avatar-proxy?u=x",
        subscriber_count=42,
    )

    resolve_calls = []

    def fake_uploads(channel_id):
        resolve_calls.append(channel_id)
        return ChannelUploads(channel_id=channel_id, title="Fresh Title", subscriber_count=7, items=[])

    monkeypatch.setattr(ga, "fetch_channel_uploads", fake_uploads)
    monkeypatch.setattr(ga, "fetch_channel_avatar", lambda channel_id: "https://example.com/avatar.jpg")
    monkeypatch.setattr(ga, "cached_avatar_or_hotlink", lambda channel_id, remote_url: "/avatar-proxy?u=fresh")

    result = ga.get_suggested_channels(db_session, [GENRE])

    # Only the never-resolved row triggered a YouTube lookup.
    assert resolve_calls == ["UCunresolved00000000"]
    by_id = {r["channel_id"]: r for r in result}
    assert by_id["UCunresolved00000000"]["title"] == "Fresh Title"
    assert by_id["UCunresolved00000000"]["subscriber_count"] == 7
    assert by_id["UCresolved000000000"]["title"] == "Cached Title"
    assert by_id["UCresolved000000000"]["subscriber_count"] == 42

    db_session.refresh(unresolved)
    assert unresolved.resolved_at is not None


def test_get_suggested_channels_falls_back_to_artist_name_on_a_dead_channel(db_session, monkeypatch):
    """A channel MusicBrainz still links but YouTube no longer serves
    shouldn't 500 the whole request — see genre_artists._resolve."""
    _row(db_session, channel_id="UCdead0000000000000", artist_name="Gone Band")

    def boom(channel_id):
        raise RuntimeError("channel not found")

    monkeypatch.setattr(ga, "fetch_channel_uploads", boom)
    monkeypatch.setattr(ga, "fetch_channel_avatar", lambda channel_id: None)

    result = ga.get_suggested_channels(db_session, [GENRE])

    assert result[0]["title"] == "Gone Band"
    row = db_session.query(GenreArtist).filter(GenreArtist.channel_id == "UCdead0000000000000").one()
    assert row.resolved_at is not None  # stamped so it isn't retried forever


def test_get_suggested_channels_empty_for_an_unseeded_genre(db_session):
    assert ga.get_suggested_channels(db_session, ["Some Genre Nobody Seeded"]) == []


def test_get_suggested_channels_empty_genres_list_short_circuits(db_session, monkeypatch):
    called = []
    monkeypatch.setattr(ga, "fetch_channel_uploads", lambda channel_id: called.append(1))

    assert ga.get_suggested_channels(db_session, []) == []
    assert called == []


def test_get_suggested_channels_resolves_at_most_the_target_across_several_genres(db_session, monkeypatch):
    """Picking several genres, each with a full unresolved cache, used to
    resolve every row of every one of them against YouTube before capping
    the output — three genres meant up to 3 * ARTISTS_PER_GENRE real
    lookups just to show ARTISTS_PER_GENRE results, which is what made the
    onboarding wizard's "Finding channels…" step feel hung for minutes on a
    multi-genre pick. Resolution has to be bounded by what's actually
    returned, not by how many genres were asked for."""
    for genre in ["Hip-Hop", "R&B", "Electronic"]:
        for i in range(ga.ARTISTS_PER_GENRE):
            db_session.add(
                GenreArtist(
                    genre=genre,
                    artist_name=f"{genre} Artist {i}",
                    channel_id=f"UC{genre[:2]}{i:016d}",
                    channel_url=f"https://www.youtube.com/channel/UC{genre[:2]}{i:016d}",
                )
            )
    db_session.commit()

    resolve_calls = []
    monkeypatch.setattr(
        ga,
        "fetch_channel_uploads",
        lambda channel_id: resolve_calls.append(channel_id)
        or ChannelUploads(channel_id=channel_id, title="T", subscriber_count=1, items=[]),
    )
    monkeypatch.setattr(ga, "fetch_channel_avatar", lambda channel_id: None)

    result = ga.get_suggested_channels(db_session, ["Hip-Hop", "R&B", "Electronic"])

    assert len(result) == ga.ARTISTS_PER_GENRE
    assert len(resolve_calls) <= ga.ARTISTS_PER_GENRE


def test_get_suggested_channels_dedupes_and_caps_at_target(db_session, monkeypatch):
    shared_channel = "UCshared0000000000"
    _row(db_session, channel_id=shared_channel, resolved=True, title="Shared", subscriber_count=1)
    for i in range(ga.ARTISTS_PER_GENRE + 3):
        db_session.add(
            GenreArtist(
                genre="Rock",
                artist_name=f"Rock Artist {i}",
                channel_id=f"UCrock{i:015d}",
                channel_url=f"https://www.youtube.com/channel/UCrock{i:015d}",
                resolved_at=utcnow(),
                title=f"Rock Artist {i}",
                subscriber_count=1,
            )
        )
    # Same channel also tagged under "Rock" in our own cache (plausible —
    # MusicBrainz can tag one artist with several genres) to check dedup
    # across the requested genre list, not just within one genre.
    db_session.add(
        GenreArtist(
            genre="Rock",
            artist_name="Shared",
            channel_id=shared_channel,
            channel_url=f"https://www.youtube.com/channel/{shared_channel}",
            resolved_at=utcnow(),
            title="Shared",
            subscriber_count=1,
        )
    )
    db_session.commit()

    result = ga.get_suggested_channels(db_session, [GENRE, "Rock"])

    assert len(result) == ga.ARTISTS_PER_GENRE
    channel_ids = [r["channel_id"] for r in result]
    assert len(channel_ids) == len(set(channel_ids))  # no duplicate channel
