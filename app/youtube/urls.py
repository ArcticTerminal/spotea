"""YouTube URL shapes and the ID conventions behind them.

Pure string work — no network, no yt-dlp, no feedparser. Everything else in
this package builds on it.
"""

import re
from urllib.parse import urlsplit

VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")

# Playlist ids come in several shapes ("PL…" user playlists, "RDCLAK5uy_…"
# auto-generated YouTube Music mixes, "UULF…" channel uploads), all longer
# than the fixed 11 characters of a video id — which is the property that
# matters here, since this guards a path parameter that gets pasted straight
# into a youtube.com URL.
PLAYLIST_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{12,64}$")

CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")

# A YouTube Music release id — an album or a single. Same reason as above:
# it arrives as untrusted path input (see routers/partials.py's release
# route) and goes straight into an API call. Length isn't fixed the way a
# channel id's is, so this bounds it rather than pinning it.
RELEASE_ID_RE = re.compile(r"^MPREb_[\w-]{1,32}$")

CHANNEL_ID_URL_RE = re.compile(r"youtube\.com/channel/(UC[\w-]{22})")
CHANNEL_ID_PARAM_RE = re.compile(r"channel_id=([\w-]+)")

YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"

# The shape a Feed row is keyed by. Nothing fetches it any more — the sync
# reads YouTube Music, not RSS (see services/artist_sync.py) — but it is
# still the canonical string two different routes into the same artist have
# to agree on, which is what stops one artist becoming two rows.
RSS_FEED_URL_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

PLAYLIST_PAGE_URL_TEMPLATE = "https://www.youtube.com/playlist?list={playlist_id}"

CHANNEL_PAGE_URL_TEMPLATE = "https://www.youtube.com/channel/{channel_id}"


# Every URL this package is handed can end up being fetched, and the only
# thing standing between "follow this channel" and
# a request-forgery primitive is the host. Without this check an authenticated
# user could point POST /feeds at 127.0.0.1 or a LAN address and read the
# failure text back; no Feed row could survive it (a real one needs a
# yt_channelid in the response), so it was a probe rather than a breach, but
# the probe is itself the leak. Checked at both entry points — resolve_feed_url,
# so the error arrives before any network call, and fetch_feed, which is what
# actually opens the socket.
_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)


def is_youtube_url(url: str) -> bool:
    """True only for an http(s) URL naming one of YouTube's own hosts.

    A scheme-less "youtube.com/@handle" is accepted by assuming https: that's
    the form a browser address bar displays, so it's the form people paste,
    and yt-dlp accepted it before this check existed. `hostname` (rather than
    `netloc`) is what makes "https://youtube.com@evil.example/" fail — the
    userinfo trick is the whole reason not to do this with a substring test.
    """
    candidate = url.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    split = urlsplit(candidate)
    if split.scheme not in ("http", "https"):
        return False
    return (split.hostname or "").lower() in _YOUTUBE_HOSTS


def extract_channel_id(url: str) -> str | None:
    """The channel id out of either shape this app passes around: the feed
    URL a Feed row is keyed by ("…?channel_id=UC…") and the channel page URL
    the Follow button carries ("youtube.com/channel/UC…")."""
    match = CHANNEL_ID_PARAM_RE.search(url) or CHANNEL_ID_URL_RE.search(url)
    return match.group(1) if match else None


def channel_feed_url(channel_id: str) -> str:
    """The canonical key for a channel — the single place this shape is
    built, so every code path that creates or upgrades a Feed row for a given
    channel_id agrees on it (see services/feed_add.py and
    routers/explore.py's _get_or_create_placeholder_feed, whose dedup lookup
    depends on this being consistent)."""
    return RSS_FEED_URL_TEMPLATE.format(channel_id=channel_id)


def playlist_url(playlist_id: str) -> str:
    """A plain playlist page, for any playlist id."""
    return PLAYLIST_PAGE_URL_TEMPLATE.format(playlist_id=playlist_id)


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


# Google's image CDN takes the rendered size in the URL's trailing "=s<n>"
# segment and resizes server-side. yt-dlp reports channel avatars as "=s0",
# which is the *original* upload — measured live, one came back at 390 KB
# for a picture the app never draws larger than 88 CSS pixels.
_AVATAR_SIZE_RE = re.compile(r"=s\d+(-[^=]*)?$")


def avatar_url_at_size(url: str | None, size: int) -> str | None:
    """The same avatar, asked for at `size` pixels instead of whatever size
    the URL currently names.

    Worth doing wherever a stored avatar URL is rendered into a small fixed
    box: at "=s0" a dozen suggestion cards pulled roughly 4.7 MB through
    /avatar-proxy, versus about 180 KB at "=s176" — same images, same CDN,
    a size parameter apart.

    Applied at render time rather than baked into what gets stored, so the
    stored URL stays exactly what YouTube reported and the display size
    remains a property of the surface drawing it.
    """
    if not url:
        return None
    return _AVATAR_SIZE_RE.sub(f"=s{size}", url)


# The same CDN, a second way of naming the size. YouTube Music reports its
# square art (song covers, album covers, playlist art, artist portraits) as
# "=w60-h60-l90-rj" rather than "=s<n>" — sometimes with an extra trailing
# segment, e.g. "=w60-h60-l90-rj-dcJRaW7REL". Unanchored and width/height
# only, so whatever follows the dimensions is preserved rather than guessed
# at; verified live that "=w544-h544-l90-rj" serves the same image at 22 KB
# where the 60px original was 1.3 KB.
_COVER_SIZE_RE = re.compile(r"=w\d+-h\d+")


def cover_url_at_size(url: str | None, size: int) -> str | None:
    """The same square artwork at `size` pixels, in whichever of the CDN's
    two size dialects the URL happens to use.

    Both turn up in one YouTube Music response: search results and artist
    portraits come back as "=w60-h60-…", while the chart shelves' playlist
    art uses plain "=s192". Handing the "=s" case straight to
    avatar_url_at_size rather than duplicating its rule is the point of
    keeping them next to each other.

    A URL with neither (i.ytimg.com video stills, which carry a signed `sqp`
    query and are not resizable this way) passes through untouched.
    """
    if not url:
        return None
    if _COVER_SIZE_RE.search(url):
        return _COVER_SIZE_RE.sub(f"=w{size}-h{size}", url, count=1)
    return avatar_url_at_size(url, size)


# YouTube Music hands back playlist ids as *browse* ids: the same id with a
# "VL" glued on the front ("VLPL…", "VLRDCLAK5uy_…"). Nothing downstream
# speaks that dialect — playlist_url() would build a youtube.com/playlist
# URL that resolves to nothing — and PLAYLIST_ID_RE happily accepts the
# prefixed form, since it is still 12-64 URL-safe characters. So the strip
# has to happen here, before the shared validation, rather than being caught
# by it.
_BROWSE_PLAYLIST_PREFIX = "VL"


def playlist_id_from_browse_id(browse_id: str | None) -> str | None:
    """A YouTube Music browse id as a plain playlist id, or None if it isn't
    one. Ids that already arrive unprefixed (the mood shelves report
    `playlistId` rather than `browseId`) pass through the same validation."""
    if not browse_id:
        return None
    playlist_id = browse_id.removeprefix(_BROWSE_PLAYLIST_PREFIX)
    return playlist_id if PLAYLIST_ID_RE.match(playlist_id) else None
