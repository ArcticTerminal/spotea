"""YouTube Music as a source for songs, playlists, charts and artists.

A sibling of search.py, not a replacement for it. The difference is the
transport: search.py drives yt-dlp, which loads and parses youtube.com
*pages*; everything here goes through ytmusicapi to YouTube Music's own
InnerTube JSON endpoint. Same company, different service, different results
— and that last part is the whole reason this module exists. Measured live:
a song search on youtube.com returns a title and a video id and nothing
else, while the same query here comes back with the artist, the album, the
duration and the release year already attached, because YouTube Music
indexes tracks rather than videos.

**Deliberately not used for podcasts, and not used for channel search.**
That is a measured decision, not an oversight. YouTube Music models a
podcast as an "MPSP…" playlist rather than a channel, which is the wrong
identity for an app whose entire sync path is a channel's RSS feed — and its
matching is worse besides: searching it for "The Diary of a CEO" put a
reupload channel whose episodes have a few hundred views ahead of the real
19M-subscriber one that youtube.com finds first. Podcast discovery and
channel search stay on search.py. See the module docstring there.

Two things about the ytmusicapi surface are worth knowing before touching
this file:

  * **Never pass `language`.** `YTMusic(language="tr")` does not fail, it
    returns *nothing* — songs, artists and albums all come back as empty
    lists while videos still work. The parser locates each result section by
    matching its translated header, and the Turkish locale ships `albümler`
    but leaves `songs` untranslated, so the match silently misses. An empty
    list is a plausible-looking answer, which is what makes it expensive.
    `location` is unaffected and safe.
  * Counts arrive as display strings ("1.8M"), not numbers — see
    _parse_count. Because the language above is always English, so is their
    formatting.

Failures are flattened the same way search.py flattens them: every function
here backs a browse surface where "found nothing" is a normal outcome the UI
already renders, so nothing raises. The catch is deliberately broad —
ytmusicapi surfaces network errors, its own YTMusicError, and plain KeyError
when YouTube reshapes a response, and none of those should turn Explore into
a 500.
"""

import logging
import threading
from dataclasses import dataclass

from ytmusicapi import YTMusic

from app.youtube.search import (
    SEARCH_RESULT_LIMIT,
    ChannelSearchResult,
    PlaylistSearchResult,
    VideoSearchResult,
    cached_avatar_or_hotlink,
)
from app.youtube.urls import (
    CHANNEL_ID_RE,
    VIDEO_ID_RE,
    absolute_thumbnail_url,
    cover_url_at_size,
    playlist_id_from_browse_id,
)

logger = logging.getLogger(__name__)

# Square art is rendered into cards roughly 200 CSS pixels wide and into the
# detail panel's hero at about twice that. 544 is a size the CDN already
# serves for album art (it is the largest YouTube Music itself asks for), so
# it costs nothing extra to standardise on. The alternative is what the API
# hands over untouched: a 60-pixel thumbnail, which is a visibly blurry card.
COVER_SIZE = 544

# YouTube Music's "global" chart, used when no country is asked for. Real
# country codes ("TR", "DE") select that country's charts instead.
GLOBAL_CHART_COUNTRY = "ZZ"

# A mood or genre shelf comes back with well over a hundred playlists in one
# response (133 for "Autumn", measured) — one shelf's worth is what gets
# rendered, so cap it here rather than serialising the rest into a cache row.
MOOD_PLAYLIST_LIMIT = 24

# The chart's artist list runs to 40. Same reasoning.
CHART_ARTIST_LIMIT = 12


# YTMusic wraps a requests.Session, which is not documented as thread-safe,
# and services/recommendations.py runs its searches across a thread pool.
# One client per thread rather than one shared behind a lock: constructing
# one costs zero network requests (it builds the session and reads a bundled
# locale file, nothing more), so the "expensive singleton" reasoning that
# would justify sharing simply does not apply here.
_local = threading.local()


def _client() -> YTMusic:
    client = getattr(_local, "client", None)
    if client is None:
        # No `language` — see the module docstring. No `location` either:
        # left empty, YouTube Music infers one from the request's own IP,
        # which for a self-hosted app is exactly the right answer and needs
        # no setting.
        client = YTMusic()
        _local.client = client
    return client


def _call(description: str, method: str, *args, **kwargs):
    """One ytmusicapi call, with failure flattened to None. See the module
    docstring for why the catch is this broad."""
    try:
        return getattr(_client(), method)(*args, **kwargs)
    except Exception:
        logger.warning("YouTube Music %s failed", description, exc_info=True)
        return None


def _search(query: str, filter: str, limit: int) -> list[dict]:
    results = _call(f"search ({filter})", "search", query, filter=filter, limit=limit) or []
    if not results:
        # Worth a line in the log rather than silence: an empty result for a
        # query that youtube.com would answer is the exact signature of the
        # `language` trap in the module docstring, and of YouTube changing a
        # section header out from under the parser.
        logger.info("YouTube Music %s search for %r returned nothing", filter, query)
    return results[:limit]


_COUNT_SUFFIXES = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def _parse_count(text: str | None) -> int | None:
    """"1.8M" as 1800000. YouTube Music reports subscriber and view counts
    only as the string it would print, while ChannelSearchResult carries a
    number (which is what the client formats for itself). Anything that
    doesn't parse becomes None — a card with no count reads fine, a card
    with a wrong one doesn't."""
    if not text:
        return None
    token = text.strip().split()[0].replace(",", "")
    multiplier = _COUNT_SUFFIXES.get(token[-1:].upper())
    if multiplier:
        token = token[:-1]
    try:
        return int(float(token) * (multiplier or 1))
    except ValueError:
        return None


def _cover_url(thumbnails: list[dict] | None) -> str | None:
    """The largest reported cover, asked for at COVER_SIZE.

    Simpler than search.py's _best_thumbnail_url, and can afford to be:
    that one exists to drop yt-dlp's speculative maxresdefault.jpg guess,
    which 404s. Every candidate here is a real URL YouTube Music itself
    reported, so the last (largest) one is always usable.
    """
    if not thumbnails:
        return None
    return absolute_thumbnail_url(cover_url_at_size(thumbnails[-1].get("url"), COVER_SIZE))


def _artist_names(item: dict) -> tuple[str | None, str | None]:
    """A track's artists as a display string, plus the channel to attach it
    to.

    The channel is the first artist's own id, which for a song is the
    auto-generated "<Artist> - Topic" channel rather than the artist's
    official channel. That is the right choice *here* even though the Topic
    channel is not what anyone wants to follow: it arrives free in this same
    response, it is a real UC id with a working RSS feed, and all it does is
    give the preview row a placeholder feed to hang off (see
    routers/explore.py's add_video_batch). Following is a separate action and
    resolves the official channel properly — see fetch_artist.
    """
    artists = [artist for artist in item.get("artists") or [] if artist.get("name")]
    if not artists:
        return None, None
    channel_id = next(
        (
            artist["id"]
            for artist in artists
            if artist.get("id") and CHANNEL_ID_RE.match(artist["id"])
        ),
        None,
    )
    return ", ".join(artist["name"] for artist in artists), channel_id


def _song_result(item: dict) -> VideoSearchResult | None:
    """One search/artist entry as a VideoSearchResult.

    Reuses search.py's dataclass rather than introducing a music-shaped one,
    which is what keeps this module a drop-in swap: routers, schemas, the
    recommendations cache payload and the client's card renderer all keep
    speaking the shape they already speak.
    """
    video_id = item.get("videoId")
    if not video_id or not VIDEO_ID_RE.match(video_id):
        return None

    title = item.get("title")
    if not title:
        return None

    channel_title, channel_id = _artist_names(item)
    return VideoSearchResult(
        video_id=video_id,
        title=title,
        thumbnail_url=_cover_url(item.get("thumbnails")),
        duration_seconds=item.get("duration_seconds"),
        channel_title=channel_title,
        channel_id=channel_id,
    )


def _song_results(items: list[dict] | None) -> list[VideoSearchResult]:
    results = (_song_result(item) for item in items or [])
    return [result for result in results if result is not None]


def _playlist_result(item: dict) -> PlaylistSearchResult | None:
    playlist_id = playlist_id_from_browse_id(item.get("browseId") or item.get("playlistId"))
    if not playlist_id:
        return None

    return PlaylistSearchResult(
        playlist_id=playlist_id,
        title=item.get("title") or "Untitled playlist",
        thumbnail_url=_cover_url(item.get("thumbnails")),
        # `author` is the publisher ("YouTube Music" for the curated ones);
        # `description` is the line YouTube Music itself prints under a mood
        # playlist, and it is the more useful of the two — "Taylor Swift,
        # Lewis Capaldi, Olivia Rodrigo" says more about a playlist called
        # "Fall Hits" than its publisher's name does. Whichever is present.
        channel_title=item.get("author") or item.get("description"),
    )


def _playlist_results(items: list[dict] | None) -> list[PlaylistSearchResult]:
    results = (_playlist_result(item) for item in items or [])
    return [result for result in results if result is not None]


def _artist_result(item: dict) -> ChannelSearchResult | None:
    """A chart or related-artist entry as a ChannelSearchResult.

    `browseId` here is the artist's YouTube Music browse id, which for an
    artist with an official channel *is* that channel's UC id — the same
    thing search.py's channel results carry, so these can share a shelf.
    Entries whose browse id isn't a channel id (a rare "MPLA…" artist page
    with no channel behind it) are dropped rather than rendered as a card
    that leads nowhere.
    """
    browse_id = item.get("browseId")
    if not browse_id or not CHANNEL_ID_RE.match(browse_id):
        return None

    title = item.get("title") or item.get("artist")
    if not title:
        return None

    return ChannelSearchResult(
        channel_id=browse_id,
        title=title,
        thumbnail_url=cached_avatar_or_hotlink(browse_id, _cover_url(item.get("thumbnails"))),
        subscriber_count=_parse_count(item.get("subscribers")),
        channel_url=f"https://www.youtube.com/channel/{browse_id}",
    )


def search_songs(query: str, limit: int = SEARCH_RESULT_LIMIT) -> list[VideoSearchResult]:
    """Explore's song search. The counterpart of search.py's search_videos,
    and the reason this module exists — see its docstring."""
    return _song_results(_search(query, "songs", limit))


def search_playlists(query: str, limit: int = SEARCH_RESULT_LIMIT) -> list[PlaylistSearchResult]:
    """Ready-made playlists for a query, preferring YouTube Music's own
    curated ones over user-made lists.

    Two filters rather than one because they answer differently: for
    "turkish rock", `featured_playlists` returns "Turkish Rock Legends" —
    curated, square cover art, actually about the genre — while `playlists`
    returns twenty community uploads of varying quality. The community
    search only runs when the curated one came up short, so the usual cost
    is still a single request.
    """
    results = _playlist_results(_search(query, "featured_playlists", limit))
    if len(results) >= limit:
        return results[:limit]

    seen = {result.playlist_id for result in results}
    for result in _playlist_results(_search(query, "playlists", limit)):
        if result.playlist_id in seen:
            continue
        results.append(result)
        if len(results) == limit:
            break
    return results


@dataclass
class Charts:
    """What one country's chart page holds: the chart playlists themselves
    ("Trending 20 Turkey", "Top 100 Songs Turkey" — ordinary playlists once
    opened, so the existing remote-playlist panel renders them with no
    special casing) and the artists currently charting there."""

    playlists: list[PlaylistSearchResult]
    artists: list[ChannelSearchResult]


def fetch_charts(
    country: str = GLOBAL_CHART_COUNTRY, artist_limit: int = CHART_ARTIST_LIMIT
) -> Charts:
    """One country's charts. Both shelves come out of a single request —
    YouTube Music returns them together, and asking twice would double the
    cost of a shelf pair nobody edits."""
    charts = _call(f"charts ({country})", "get_charts", country) or {}
    artists = (_artist_result(item) for item in charts.get("artists") or [])
    return Charts(
        playlists=_playlist_results(charts.get("videos")),
        artists=[artist for artist in artists if artist is not None][:artist_limit],
    )


@dataclass
class MoodCategory:
    """One entry of YouTube Music's mood/genre browse menu. `params` is an
    opaque token from that same response — the only way to ask for the
    category's playlists, and not something that can be constructed."""

    title: str
    params: str
    section: str


# YouTube Music's mood menu has two sections and only one of them is usable.
# Measured 2026-08-19 across all 40 categories: 25 of them raise a parse
# error from inside ytmusicapi, and they are every single entry under
# "Genres" ("Rock", "Jazz", "Latin", …) plus one mood ("Family"). Those
# grids mix plain videos in among the playlists, and the library's playlist
# parser reads a browse id off a title run those video items don't have —
# so it isn't a bad category, it's a shape its parser doesn't handle, and
# nothing short of reimplementing that parser works around it here.
# Defaulting to the section that works keeps the shelf reliable; pass
# section=None to get everything back once upstream can parse it.
MOOD_SECTION = "Moods & moments"


def fetch_mood_categories(section: str | None = MOOD_SECTION) -> list[MoodCategory]:
    """The moods (and, asked for, genres) YouTube Music currently offers,
    flattened out of its two sections but keeping which one each came from,
    so a caller can present them apart."""
    sections = _call("mood categories", "get_mood_categories") or {}
    return [
        MoodCategory(title=item["title"], params=item["params"], section=name)
        for name, items in sections.items()
        if section is None or name == section
        for item in items
        if item.get("title") and item.get("params")
    ]


def fetch_mood_playlists(
    params: str, limit: int = MOOD_PLAYLIST_LIMIT
) -> list[PlaylistSearchResult]:
    """One mood or genre's playlists. `params` comes from
    fetch_mood_categories and is opaque — see MoodCategory."""
    return _playlist_results(_call("mood playlists", "get_mood_playlists", params))[:limit]


@dataclass
class ArtistProfile:
    """An artist page as YouTube Music knows it.

    `browse_id` is how YouTube Music addresses the artist (in practice the
    Topic channel's id, which is also what every song result attributes its
    tracks to); `channel_id` is the artist's **official** YouTube channel —
    a genuinely different id, and the one worth following, since it carries
    the real uploads an RSS feed can sync. Resolving one to the other is the
    only reason to make this call for a follow action.
    """

    browse_id: str
    channel_id: str | None
    name: str
    description: str | None
    subscriber_count: int | None
    monthly_listeners: str | None
    avatar_url: str | None
    tracks: list[VideoSearchResult]


def fetch_artist(browse_id: str) -> ArtistProfile | None:
    """One artist's page, or None if YouTube Music has no such artist.

    `tracks` merges the page's top songs with its videos, deduplicated by
    video id — the two lists overlap in what they are *about* but not in
    what they point at, since YouTube Music holds a separate id for a song's
    audio track (MUSIC_VIDEO_TYPE_ATV) and its official music video
    (…_OMV). Both are playable and either may be the only one that exists
    for a given release, so both go in, audio first.
    """
    artist = _call("artist", "get_artist", browse_id)
    if not artist or not artist.get("name"):
        return None

    tracks: list[VideoSearchResult] = []
    seen: set[str] = set()
    for section in ("songs", "videos"):
        for track in _song_results((artist.get(section) or {}).get("results")):
            if track.video_id in seen:
                continue
            seen.add(track.video_id)
            tracks.append(track)

    channel_id = artist.get("channelId")
    return ArtistProfile(
        browse_id=browse_id,
        channel_id=channel_id if channel_id and CHANNEL_ID_RE.match(channel_id) else None,
        name=artist["name"],
        description=artist.get("description"),
        subscriber_count=_parse_count(artist.get("subscribers")),
        monthly_listeners=artist.get("monthlyListeners"),
        avatar_url=_cover_url(artist.get("thumbnails")),
        tracks=tracks,
    )


def resolve_artist_channel(browse_id: str) -> str | None:
    """The official YouTube channel behind an artist's Topic channel, for
    turning "follow this artist" into a channel this app can actually sync.

    Verified live: the Topic id every song result carries
    ("UCNaGLJRPE3ohleIDM7RFtlQ") resolves to "UC6OI7Crv96jgra5pwJNDFRQ",
    which is @sezenaksu — 3.19M subscribers and a real upload feed, where
    the Topic channel is an automated container nobody would choose to
    follow. Costs one request, which is why it happens on the follow click
    and not on every search result.
    """
    artist = fetch_artist(browse_id)
    return artist.channel_id if artist else None
