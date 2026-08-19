"""Onboarding's genre-artist cache (app/services/genre_artists.py).

Every MusicBrainz and YouTube call is monkeypatched out — real network here
would make the suite slow, flaky, and (for the YouTube half) a genuine
rate-limiting liability on a residential IP, same reasoning as
test_recommendations.py's fake_search fixture.
"""

from app.models import GenreArtist
from app.services import genre_artists as ga
from app.timeutil import utcnow
from app.youtube.search import ChannelProfile

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


def test_suggestions_never_call_youtube(db_session, monkeypatch):
    """The whole point of the change this test guards.

    Display metadata used to be resolved lazily, in-request, the first time
    any profile picked a given genre — two live yt-dlp calls per channel,
    twelve channels per pick, which left the wizard on "Finding channels…"
    for minutes. It is a pure database read now; the resolution happens
    offline in scripts/resolve_genre_artists.py and ships committed.
    """
    _row(db_session, channel_id="UCunresolved00000000")
    _row(db_session, channel_id="UCresolved000000000", resolved=True, title="Cached Title")

    def fail_if_called(channel_id):
        raise AssertionError("serving suggestions must not touch YouTube")

    monkeypatch.setattr(ga, "fetch_channel_profile", fail_if_called)

    [group] = ga.get_suggested_channels_by_genre(db_session, [GENRE])

    assert group["genre"] == GENRE
    assert {c["channel_id"] for c in group["channels"]} == {
        "UCunresolved00000000",
        "UCresolved000000000",
    }


def test_suggestions_put_resolved_rows_first(db_session):
    """A row still waiting on the generator has no avatar and only its
    curated name, so it fills the block after the ones that can render
    properly — but it does still appear, rather than vanishing."""
    _row(db_session, channel_id="UCunresolved00000000", artist_name="Not Resolved Yet")
    _row(
        db_session,
        channel_id="UCresolved000000000",
        artist_name="Resolved",
        resolved=True,
        title="ResolvedVEVO",
        thumbnail_url="https://yt3.ggpht.com/r=s0",
    )

    channels = ga.get_suggested_channels_by_genre(db_session, [GENRE])[0]["channels"]

    assert [c["title"] for c in channels] == ["Resolved", "Not Resolved Yet"]
    assert channels[1]["thumbnail_url"] is None


def test_a_card_shows_the_curated_name_not_the_channels_own_title(db_session):
    """The channel's own title is the noisy one on the music side —
    "SnoopDoggVEVO" for Snoop Dogg — and long enough to be truncated on a card
    sized for a name. The curated string is what a person calls that artist,
    and choosing it is what curating one is for; row.title stays proof the
    resolved channel is the right one."""
    _row(
        db_session,
        channel_id="UCsnoop00000000000",
        artist_name="Snoop Dogg",
        resolved=True,
        title="SnoopDoggVEVO",
    )

    channels = ga.get_suggested_channels_by_genre(db_session, [GENRE])[0]["channels"]

    assert channels[0]["title"] == "Snoop Dogg"


def test_suggested_channels_proxy_the_stored_remote_avatar_url(db_session):
    """thumbnail_url holds the raw upstream URL (that is what makes it
    committable); turning it into something the browser can load without
    tripping Chrome's ORB is a read-time concern."""
    _row(
        db_session,
        channel_id="UCavatar00000000000",
        resolved=True,
        title="With Avatar",
        thumbnail_url="https://yt3.ggpht.com/abc=s0",
    )

    channels = ga.get_suggested_channels_by_genre(db_session, [GENRE])[0]["channels"]

    # Asked for at the size the card actually draws, not the "=s0" original
    # YouTube reports — see SUGGESTION_AVATAR_SIZE.
    assert channels[0]["thumbnail_url"] == "/avatar-proxy?u=https%3A%2F%2Fyt3.ggpht.com%2Fabc%3Ds176"


def test_suggested_channels_never_report_a_subscriber_count(db_session):
    """Never displayed in the wizard, and deliberately not revived: the
    lazy resolution this replaced read it off the uploads playlist, which
    carries none, so the cards have never shown one. The channel page does
    carry it, so filling it in would silently add a number — frozen at
    generation time — that nobody asked for."""
    _row(db_session, channel_id="UCsubs000000000000", resolved=True, title="T", subscriber_count=999)

    channels = ga.get_suggested_channels_by_genre(db_session, [GENRE])[0]["channels"]

    assert channels[0]["subscriber_count"] is None


def test_suggestions_are_empty_for_an_unseeded_genre(db_session):
    assert ga.get_suggested_channels_by_genre(db_session, ["Some Genre Nobody Seeded"]) == []


def test_suggestions_empty_genres_list_short_circuits(db_session):
    assert ga.get_suggested_channels_by_genre(db_session, []) == []


def _seed_genres(db_session, genres, per_genre):
    for g, genre in enumerate(genres):
        for i in range(per_genre):
            channel_id = f"UC{g:04d}{i:014d}"
            db_session.add(
                GenreArtist(
                    genre=genre,
                    artist_name=f"{genre} Artist {i}",
                    channel_id=channel_id,
                    channel_url=f"https://www.youtube.com/channel/{channel_id}",
                    resolved_at=utcnow(),
                )
            )
    db_session.commit()


def test_every_picked_genre_gets_its_own_block_in_pick_order(db_session):
    """The reason suggestions are grouped at all: several picks used to be
    round-robined into one twelve-long shelf, so each genre contributed two
    unlabelled channels. Each pick gets a block of its own now."""
    picked = ["Hip-Hop", "R&B", "Electronic"]
    _seed_genres(db_session, picked, ga.ARTISTS_PER_GENRE)

    groups = ga.get_suggested_channels_by_genre(db_session, picked)

    assert [g["genre"] for g in groups] == picked
    for group in groups:
        assert len(group["channels"]) == ga.ARTISTS_PER_GENRE
        # Every channel in a block really comes from that block's genre.
        assert all(c["title"].startswith(group["genre"]) for c in group["channels"])


def test_many_picks_each_keep_their_whole_shelf(db_session):
    """Nothing is traded away as more genres are picked: each one is its own
    horizontally scrolling shelf, so twenty picks are twenty full shelves
    rather than twenty thin slices of one."""
    picked = [f"Genre {i}" for i in range(20)]
    _seed_genres(db_session, picked, ga.ARTISTS_PER_GENRE)

    groups = ga.get_suggested_channels_by_genre(db_session, picked)

    assert len(groups) == len(picked)
    assert all(len(g["channels"]) == ga.ARTISTS_PER_GENRE for g in groups)


def test_a_single_pick_gets_the_full_seeded_dozen(db_session):
    _seed_genres(db_session, ["Jazz"], ga.ARTISTS_PER_GENRE)

    [group] = ga.get_suggested_channels_by_genre(db_session, ["Jazz"])

    assert len(group["channels"]) == ga.ARTISTS_PER_GENRE


def test_a_channel_seeded_under_two_picks_is_only_offered_once(db_session):
    """Same "Add" button twice on one screen otherwise — an act curated
    under both Hip-Hop and R&B is a normal case, not an edge one."""
    shared = "UCshared0000000000"
    for genre in ["Hip-Hop", "R&B"]:
        db_session.add(
            GenreArtist(
                genre=genre,
                artist_name="Shared Act",
                channel_id=shared,
                channel_url=f"https://www.youtube.com/channel/{shared}",
                resolved_at=utcnow(),
            )
        )
    db_session.commit()

    groups = ga.get_suggested_channels_by_genre(db_session, ["Hip-Hop", "R&B"])

    # The second genre had nothing left of its own, so it contributes no
    # empty block either.
    assert [g["genre"] for g in groups] == ["Hip-Hop"]
    assert [c["channel_id"] for c in groups[0]["channels"]] == [shared]


def test_build_row_applies_a_committed_profile(db_session):
    row = ga.build_row("Jazz", "Miles Davis", "UCmiles000000000000", ("Miles Davis", "https://yt3.ggpht.com/m=s0"))

    assert row.title == "Miles Davis"
    assert row.thumbnail_url == "https://yt3.ggpht.com/m=s0"
    assert row.resolved_at is not None


def test_build_row_leaves_a_channel_with_no_profile_unresolved(db_session):
    """Just added to a curated list, generator not re-run yet. It has to be
    left unresolved rather than stamped with a half-filled row, since that
    flag is what get_suggested_channels_by_genre sorts on."""
    row = ga.build_row("Jazz", "New Act", "UCnew00000000000000", None)

    assert row.title == "New Act"  # the curated name still shows
    assert row.thumbnail_url is None
    assert row.resolved_at is None


def test_fetch_profile_reads_a_channel_in_one_call(monkeypatch):
    """One channel-page read covers name and avatar together. It used to
    take two calls, the second of which pulled a 50-item uploads playlist
    and threw everything but the title away."""
    calls = []
    monkeypatch.setattr(
        ga,
        "fetch_channel_profile",
        lambda channel_id: calls.append(channel_id)
        or ChannelProfile(
            channel_id=channel_id, title="Real Name", subscriber_count=1000, avatar_url="https://a=s0"
        ),
    )

    profile = ga.fetch_profile("UCone000000000000000")

    assert calls == ["UCone000000000000000"]
    assert profile.title == "Real Name"


def test_fetch_profile_is_none_when_the_channel_does_not_answer(monkeypatch):
    """None rather than raising: the generator runs this over several
    hundred channels, and one dead entry must not end the run."""

    def boom(channel_id):
        raise RuntimeError("channel not found")

    monkeypatch.setattr(ga, "fetch_channel_profile", boom)

    assert ga.fetch_profile("UCdead0000000000000") is None


def test_apply_profile_stores_the_remote_url_not_a_display_one(db_session):
    row = _row(db_session, channel_id="UCone000000000000000")

    applied = ga.apply_profile(
        row,
        ChannelProfile(
            channel_id=row.channel_id,
            title="Real Name",
            subscriber_count=1000,
            avatar_url="https://yt3.ggpht.com/a=s0",
        ),
    )

    assert applied is True
    assert row.title == "Real Name"
    # Stored raw — wrapping and sizing it for display happens at read time.
    assert row.thumbnail_url == "https://yt3.ggpht.com/a=s0"
    # Fetched (the channel page carries one) but deliberately not kept.
    assert row.subscriber_count is None
    assert row.resolved_at is not None


def test_apply_profile_falls_back_to_the_curated_name_on_a_dead_channel(db_session):
    """Still stamped resolved, so the generator doesn't retry a channel that
    has already been shown not to answer on every future run."""
    row = _row(db_session, channel_id="UCdead0000000000000", artist_name="Gone Band")

    assert ga.apply_profile(row, None) is False
    assert row.title == "Gone Band"
    assert row.thumbnail_url is None
    assert row.resolved_at is not None


def test_a_shelf_carries_everything_seeded_for_its_genre(db_session):
    """No cap here at all: how much a genre is worth showing is decided by
    how many channels are curated into it, and a shelf that overflows scrolls
    sideways rather than growing the step."""
    _seed_genres(db_session, ["Rock"], ga.ARTISTS_PER_GENRE + 3)

    [group] = ga.get_suggested_channels_by_genre(db_session, ["Rock"])

    assert len(group["channels"]) == ga.ARTISTS_PER_GENRE + 3
