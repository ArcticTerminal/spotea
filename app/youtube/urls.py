"""YouTube URL shapes and the ID conventions behind them.

Pure string work — no network, no yt-dlp, no feedparser. Everything else in
this package builds on it.
"""

import re

VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")

# Playlist ids come in several shapes ("PL…" user playlists, "RDCLAK5uy_…"
# auto-generated YouTube Music mixes, "UULF…" channel uploads), all longer
# than the fixed 11 characters of a video id — which is the property that
# matters here, since this guards a path parameter that gets pasted straight
# into a youtube.com URL.
PLAYLIST_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{12,64}$")

# Fetching a channel's Videos-tab playlist (UULF, see longform_feed_url)
# instead of its full-uploads feed is supposed to exclude Shorts entirely —
# but that's an unofficial, undocumented YouTube convention, and in practice
# a Short occasionally still turns up there anyway. This is a defensive
# second check applied at insert time (see feed_sync.apply_feed_data and
# services/backfill.py) using each entry's already-fetched duration, not a
# replacement for the UULF trick — classic Shorts are ≤60s; YouTube widened
# the format to up to 3 minutes in 2024, but that upper range overlaps with
# plenty of legitimate short-form long-content videos, so this stays
# conservative rather than risk dropping real content.
SHORT_MAX_DURATION_SECONDS = 60

# A bare channel id, for the places one arrives as untrusted input rather
# than being pulled out of a URL we fetched (see routers/partials.py's remote
# channel route, which interpolates it straight into a youtube.com URL).
CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")

CHANNEL_ID_URL_RE = re.compile(r"youtube\.com/channel/(UC[\w-]{22})")
CHANNEL_ID_PARAM_RE = re.compile(r"channel_id=([\w-]+)")

YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"

RSS_FEED_URL_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
RSS_FEED_PLAYLIST_URL_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}"
PLAYLIST_PAGE_URL_TEMPLATE = "https://www.youtube.com/playlist?list={playlist_id}"

# sp=EgIQAg%3D%3D restricts YouTube search results to the "Channel" type.
CHANNEL_SEARCH_URL_TEMPLATE = "https://www.youtube.com/results?search_query={query}&sp=EgIQAg%3D%3D"

# No type filter — Explore is searching for a specific video/song, and
# YouTube's default mixed results are already mostly videos. Adding a
# "Video" type filter isn't needed on top of that.
VIDEO_SEARCH_URL_TEMPLATE = "https://www.youtube.com/results?search_query={query}"

# sp=EgIQAw%3D%3D restricts YouTube search results to the "Playlist" type —
# same trick as the channel filter above, one value along.
PLAYLIST_SEARCH_URL_TEMPLATE = "https://www.youtube.com/results?search_query={query}&sp=EgIQAw%3D%3D"

CHANNEL_PAGE_URL_TEMPLATE = "https://www.youtube.com/channel/{channel_id}"


def extract_channel_id(rss_url: str) -> str | None:
    match = CHANNEL_ID_PARAM_RE.search(rss_url)
    return match.group(1) if match else None


def _playlist_id(channel_id: str, prefix: str) -> str:
    """YouTube maps a channel to several special playlists by swapping the
    "UC" prefix of its channel ID: "UULF" is the Videos tab (long-form only,
    Shorts and Lives excluded), "UU" is all uploads mixed together. This is
    an undocumented but long-stable YouTube convention, not a public API."""
    return prefix + channel_id[2:] if channel_id.startswith("UC") else channel_id


def channel_feed_url(channel_id: str) -> str:
    """Canonical RSS feed URL for a channel_id — the single place this shape
    is built, so every code path that creates or upgrades a Feed row for a
    given channel_id agrees on its rss_url (see extract.resolve_feed_url and
    routers/explore.py's _get_or_create_placeholder_feed, whose dedup lookup
    depends on this being consistent)."""
    return RSS_FEED_URL_TEMPLATE.format(channel_id=channel_id)


def longform_feed_url(channel_id: str) -> str:
    """RSS feed scoped to a channel's Videos tab (UULF playlist) instead of
    all uploads — entries here are never Shorts, so callers don't need a
    separate Shorts-tab fetch to filter them out."""
    return RSS_FEED_PLAYLIST_URL_TEMPLATE.format(playlist_id=_playlist_id(channel_id, "UULF"))


def playlist_url(playlist_id: str) -> str:
    """A plain playlist page, for any playlist id — a recommended playlist
    from search, as opposed to the channel-derived one longform_playlist_url
    below builds."""
    return PLAYLIST_PAGE_URL_TEMPLATE.format(playlist_id=playlist_id)


def longform_playlist_url(channel_id: str) -> str:
    """The same Videos-tab playlist as longform_feed_url, but as a regular
    playlist page for yt-dlp to extract rather than an RSS feed. Always
    newest-first, unlike the /videos tab whose sort order some channels
    override (e.g. "Popular"), which can leave recent uploads out of a
    bounded flat fetch."""
    return PLAYLIST_PAGE_URL_TEMPLATE.format(playlist_id=_playlist_id(channel_id, "UULF"))


def absolute_thumbnail_url(raw: str | None) -> str | None:
    if not raw:
        return None
    url = f"https:{raw}" if raw.startswith("//") else raw
    # yt3.googleusercontent.com serves the same images (same path) as
    # yt3.ggpht.com, but browsers hotlinking it cross-origin in an <img> tag
    # get net::ERR_BLOCKED_BY_ORB (Chrome's Opaque Response Blocking) — the
    # googleusercontent.com response is missing the headers ORB wants to
    # confirm it's really an image. ggpht.com (YouTube's dedicated image
    # CDN) sends them and renders fine, so rewrite to that host.
    return url.replace("//yt3.googleusercontent.com/", "//yt3.ggpht.com/")
