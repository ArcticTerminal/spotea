"""YouTube search and playlist browsing, for everything Explore shows.

Same tool as extract.py (yt-dlp, flat extraction) but a different trigger:
these run behind Explore's search box, which fires while someone is typing —
so searches are debounced client-side, capped at SEARCH_RESULT_LIMIT, and
return an empty result rather than raising when YouTube says no. A failed
search should show "nothing found", not an error page. Recommendations
(services/recommendations.py) reuse the same functions and want the same
behaviour: one interest coming back empty shouldn't sink the whole batch.
"""

import urllib.parse
from dataclasses import dataclass

import yt_dlp

from app.images import cached_avatar_path
from app.youtube.extract import NETWORK_OPTS
from app.youtube.urls import (
    CHANNEL_SEARCH_URL_TEMPLATE,
    PLAYLIST_ID_RE,
    PLAYLIST_SEARCH_URL_TEMPLATE,
    VIDEO_ID_RE,
    VIDEO_SEARCH_URL_TEMPLATE,
    absolute_thumbnail_url,
    longform_playlist_url,
    playlist_url,
)

SEARCH_RESULT_LIMIT = 8

# Opening a recommended playlist is a deliberate click, not a keystroke, so
# it can afford a deeper fetch than a search — but still bounded: some
# playlists run to thousands of entries, and this list is rendered in one go.
PLAYLIST_ITEM_LIMIT = 50


def _flat_opts(limit: int) -> dict:
    # NETWORK_OPTS bounds the call — search runs on a request thread from a
    # keystroke, so an unbounded one is the worst place for it. See its
    # definition in extract.py.
    return {
        **NETWORK_OPTS,
        "extract_flat": "in_playlist",
        "playlist_items": f"1-{limit}",
    }

@dataclass
class ChannelSearchResult:
    channel_id: str
    title: str
    thumbnail_url: str | None
    subscriber_count: int | None
    channel_url: str


@dataclass
class VideoSearchResult:
    video_id: str
    title: str
    thumbnail_url: str | None
    duration_seconds: int | None
    channel_title: str | None
    # Present on playlist/channel entries, where it's the real uploader;
    # unreliable (and often absent) on flat *search* entries, which is why
    # add_single_video still resolves its own rather than trusting this. What
    # it buys is POST /feeds/videos/batch: a whole remote list can be turned
    # into rows without one network call.
    channel_id: str | None = None


@dataclass
class PlaylistSearchResult:
    playlist_id: str
    title: str
    thumbnail_url: str | None
    # Whoever published the playlist ("YouTube Music" for the auto-generated
    # mixes, a real channel otherwise). No video count: YouTube's search
    # results don't carry one, and a field that's always None is worse than
    # no field — the count is only known once the playlist itself is opened
    # (see fetch_playlist).
    channel_title: str | None


@dataclass
class PlaylistDetail:
    playlist_id: str
    title: str | None
    video_count: int | None
    items: list[VideoSearchResult]


@dataclass
class ChannelUploads:
    channel_id: str
    title: str | None
    subscriber_count: int | None
    items: list[VideoSearchResult]


def _best_thumbnail_url(thumbnails: list[dict]) -> str | None:
    """The largest *usable* thumbnail from a flat entry.

    "Usable" is doing real work here. YouTube serves an auto-generated mix's
    artwork from i9.ytimg.com/s_p/… behind a signed `sqp` query, and yt-dlp
    appends an unsigned maxresdefault.jpg guess as the largest candidate —
    which 404s, so taking thumbnails[-1] leaves every YouTube Music playlist
    card with a broken image. When any candidate carries a query string, the
    ones without it are that speculation and get dropped; entries where none
    do (channel avatars, which are plain ggpht.com paths) are unaffected.
    """
    if not thumbnails:
        return None
    signed = [thumbnail for thumbnail in thumbnails if "?" in (thumbnail.get("url") or "")]
    return absolute_thumbnail_url((signed or thumbnails)[-1].get("url"))


def _extract_flat(url: str, limit: int) -> dict:
    """Flat extraction of any YouTube page, with failures flattened to an
    empty result. Every caller here is a browse/discovery surface where "found
    nothing" is a normal outcome the UI already renders — see this module's
    docstring."""
    try:
        with yt_dlp.YoutubeDL(_flat_opts(limit)) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except yt_dlp.utils.DownloadError:
        return {}


def _search_entries(search_url: str) -> list[dict]:
    return _extract_flat(search_url, SEARCH_RESULT_LIMIT).get("entries") or []


def _video_result(entry: dict) -> VideoSearchResult | None:
    """One flat entry as a VideoSearchResult, or None if it isn't a video.

    Shared by video search and playlist extraction because the two produce
    the same entry shape — and the same "RD"-prefixed Mix/Radio pseudo-entries
    that need dropping, which the 11-character id check does for free.
    """
    video_id = entry.get("id")
    if not video_id or not VIDEO_ID_RE.match(video_id):
        return None

    # A video YouTube still lists (same item count) but has gone private,
    # been deleted, or lost its channel to a terminated account comes back
    # from flat extraction with no title at all — every video actually worth
    # showing has one, even a live premiere with no duration yet. That's a
    # cleaner signal than trying to download it later and finding out then:
    # skip it here rather than surface it as a playable "Untitled" result
    # that only errors once picked.
    title = entry.get("title")
    if not title:
        return None

    duration = entry.get("duration")
    return VideoSearchResult(
        video_id=video_id,
        title=title,
        thumbnail_url=_best_thumbnail_url(entry.get("thumbnails") or []),
        duration_seconds=int(duration) if isinstance(duration, (int, float)) else None,
        channel_title=entry.get("channel"),
        channel_id=entry.get("channel_id"),
    )


def _cached_avatar_or_hotlink(channel_id: str, remote_url: str | None) -> str | None:
    """A search result's avatar — reused from disk if this channel already
    has one (already followed, or found in an earlier search), hotlinked
    straight from YouTube otherwise. This used to download a fresh copy for
    every result instead: measured live, 977 of 1060 avatar files on disk
    (92%, 16.4 MB) were exactly that — orphans with no Feed row ever
    pointing at them, because nothing anywhere ever deletes an avatar. Per
    the locked decision, only a channel someone actually follows earns a
    local copy now (see feed_sync.fetch_feed_data); Explore search reuses
    what exists without creating more. `remote_url` already went through
    absolute_thumbnail_url's yt3.ggpht.com rewrite (see _best_thumbnail_url),
    which is what makes hotlinking it viable at all — the alternative,
    yt3.googleusercontent.com, gets blocked by Chrome's Opaque Response
    Blocking when hotlinked. That rewrite doesn't eliminate ORB for every
    case, so some never-followed channels may still render with no avatar —
    a knowingly accepted tradeoff, not a bug.
    """
    if not remote_url:
        return None
    return cached_avatar_path(channel_id) or remote_url


def search_channels(query: str) -> list[ChannelSearchResult]:
    search_url = CHANNEL_SEARCH_URL_TEMPLATE.format(query=urllib.parse.quote(query))

    results = []
    for entry in _search_entries(search_url):
        channel_id = entry.get("channel_id") or entry.get("id")
        if not channel_id:
            continue
        remote_thumbnail_url = _best_thumbnail_url(entry.get("thumbnails") or [])
        results.append(
            ChannelSearchResult(
                channel_id=channel_id,
                title=entry.get("title") or entry.get("channel") or channel_id,
                thumbnail_url=_cached_avatar_or_hotlink(channel_id, remote_thumbnail_url),
                subscriber_count=entry.get("channel_follower_count"),
                channel_url=entry.get("channel_url")
                or entry.get("url")
                or f"https://www.youtube.com/channel/{channel_id}",
            )
        )
    return results


def search_videos(query: str) -> list[VideoSearchResult]:
    """Finds a specific video/song rather than a channel to follow. Unlike
    search_channels, no avatar download step: video thumbnails are already
    hotlinked directly from i.ytimg.com elsewhere in the app (see
    _content_card.html), with none of the ORB blocking that makes channel
    avatars need the ggpht.com rewrite/local download."""
    search_url = VIDEO_SEARCH_URL_TEMPLATE.format(query=urllib.parse.quote(query))
    results = (_video_result(entry) for entry in _search_entries(search_url))
    return [result for result in results if result is not None]


def search_playlists(query: str) -> list[PlaylistSearchResult]:
    """Finds ready-made playlists — both hand-made "PL…" ones and YouTube's
    own auto-generated "RDCLAK5uy_…" genre mixes, which is what a genre-shaped
    query mostly turns up. Only reachable through Explore's recommendations
    (see services/recommendations.py); the search box itself stays songs +
    channels, since that's what a typed query is nearly always after.

    No thumbnail download step, same as search_videos: playlist artwork is
    served from ytimg.com, which the app already hotlinks."""
    search_url = PLAYLIST_SEARCH_URL_TEMPLATE.format(query=urllib.parse.quote(query))

    results: list[PlaylistSearchResult] = []
    for entry in _search_entries(search_url):
        playlist_id = entry.get("id")
        if not playlist_id or not PLAYLIST_ID_RE.match(playlist_id):
            continue

        results.append(
            PlaylistSearchResult(
                playlist_id=playlist_id,
                title=entry.get("title") or "Untitled playlist",
                thumbnail_url=_best_thumbnail_url(entry.get("thumbnails") or []),
                channel_title=entry.get("channel"),
            )
        )

    return results


def _flat_video_results(info: dict) -> list[VideoSearchResult]:
    """Every playable entry of a flat playlist/channel read, with each one's
    channel filled in from the page itself where YouTube left it off.

    That last part is load-bearing rather than cosmetic. YouTube repeats a
    per-entry channel on a *mixed* playlist ("Best of Turkish Rock", tracks
    from many uploaders) but omits it entirely on a single-uploader one (a
    course playlist, or any channel's own uploads) — every entry there comes
    back with channel_id and channel both None. Since that's exactly the case
    where the playlist's own owner *is* the uploader, taking it from the page
    is correct, not a guess.

    Without it those rows reach the client with no channel id, the batch
    endpoint refuses them (routers/explore.py's add_video_batch), and a
    perfectly good playlist reports "Nothing to play here" — which is how this
    surfaced. Resolving each video individually instead would be one yt-dlp
    call per track, which is the cost the batch design exists to avoid.
    """
    results = [_video_result(entry) for entry in info.get("entries") or []]
    items = [result for result in results if result is not None]

    owner_id = info.get("channel_id")
    owner_title = info.get("channel") or info.get("uploader")
    if owner_id:
        for item in items:
            if not item.channel_id:
                item.channel_id = owner_id
                item.channel_title = item.channel_title or owner_title

    return items


def fetch_playlist(playlist_id: str) -> PlaylistDetail:
    """One playlist's first PLAYLIST_ITEM_LIMIT tracks — what the detail panel
    renders when a recommended playlist is opened (see
    services/remote_detail.py)."""
    info = _extract_flat(playlist_url(playlist_id), PLAYLIST_ITEM_LIMIT)

    return PlaylistDetail(
        playlist_id=playlist_id,
        title=info.get("title"),
        # The playlist's real length, not len(items) — which is capped above,
        # so "50 of 320" is worth being able to say.
        video_count=info.get("playlist_count"),
        items=_flat_video_results(info),
    )


def fetch_channel_uploads(channel_id: str) -> ChannelUploads:
    """A channel's most recent long-form uploads, for previewing a channel
    nobody follows yet.

    Reads the same UULF playlist the backfill scan does (newest-first, Shorts
    already excluded — see urls.longform_playlist_url) rather than the RSS
    feed, because RSS caps out at 15 entries and carries no durations."""
    info = _extract_flat(longform_playlist_url(channel_id), PLAYLIST_ITEM_LIMIT)
    items = _flat_video_results(info)
    # _flat_video_results already fills these in from the page where it can;
    # this covers the case where the UULF playlist response itself carries no
    # channel_id. Here the answer is never in doubt — every entry of a
    # channel's own uploads belongs to the channel that was asked for.
    for item in items:
        item.channel_id = item.channel_id or channel_id

    return ChannelUploads(
        channel_id=channel_id,
        # The uploads playlist is titled "Videos"; the channel's own name
        # comes through as `channel` on the playlist info.
        title=info.get("channel") or info.get("uploader"),
        subscriber_count=info.get("channel_follower_count"),
        items=items,
    )
