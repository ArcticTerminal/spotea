"""Guards on the shipped JavaScript that Python tests can still enforce.

There is no JS test runner in this project (no package.json, no node_modules)
and adding one — plus a browser — for a handful of assertions costs more than
it returns. So these are source-level guards, not behavioural tests: they
check that a specific, previously-shipped mistake cannot come back, and they
say so rather than pretending to execute anything.
"""

import re
from pathlib import Path

CORE_JS = Path("app/static/js/core.js")
JS_DIR = Path("app/static/js")


def _function_body(source: str, name: str) -> str:
    """The text of `export function <name>(...) { ... }`, brace-matched."""
    start = source.index(f"export function {name}(")
    open_brace = source.index("{", start)
    depth = 0
    for index in range(open_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace : index + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def test_escape_html_escapes_quotes() -> None:
    """escapeHtml has to be safe in attribute position, not just in text.

    It was written as `div.textContent = str; return div.innerHTML`, which
    escapes `&`, `<` and `>` and nothing else — serializing a text node has no
    reason to touch quotes. Every caller interpolates the result into a
    double-quoted attribute (data-title, aria-label, title, src), so a YouTube
    title containing `"` closed the attribute and everything after it parsed
    as further attributes on the same element. Confirmed executable: an
    injected `onerror` on the card's <img> fired on render, with no
    interaction. Ordinary titles carry quotes constantly, so this also
    corrupted plain markup.
    """
    body = _function_body(CORE_JS.read_text(), "escapeHtml")

    for char, entity in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"), ("'", "&#39;")):
        assert entity in body, f"escapeHtml no longer escapes {char!r} to {entity}"

    # The specific implementation that was unsafe. textContent/innerHTML is a
    # reasonable-looking way to write this and is exactly what came back.
    assert "innerHTML" not in body, "escapeHtml is back to the textContent/innerHTML form, which leaves quotes intact"


def test_escape_html_is_the_only_escaper_used_in_markup() -> None:
    """No module builds attribute markup with a second, hand-rolled escaper.

    The fix above is only worth anything if there is one escaper to fix. A
    module that grows its own (or interpolates raw) puts the same hole back
    somewhere this test cannot see.
    """
    offenders: list[str] = []
    for path in sorted(JS_DIR.rglob("*.js")):
        source = path.read_text()
        # `="${...}"` interpolations inside a template literal, i.e. a value
        # landing in attribute position.
        for match in re.finditer(r'="\$\{([^}]+)\}"', source):
            expression = match.group(1)
            if "escapeHtml" in expression:
                continue
            # Numeric/enum-ish values that cannot carry a quote: `?? ""`
            # fallbacks over ids and durations, and String(bool) conversions.
            if re.fullmatch(r"[\w.?\s]*(\?\?\s*\"\")?", expression):
                continue
            offenders.append(f"{path}: {match.group(0)}")

    assert not offenders, "attribute interpolation without escapeHtml:\n" + "\n".join(offenders)


def test_service_worker_ignores_cross_origin_requests() -> None:
    """The worker must not intercept anything it doesn't serve itself.

    It used to handle every GET, including the no-cors `<img>` requests for
    remote artwork on i.ytimg.com. Passing an opaque response through its
    fetch/clone/cache.put path made those fail outright: measured against the
    live app, 23 of Explore's thumbnails failed with ERR_FAILED while the
    worker was registered and none did with it blocked. So uncached Explore
    artwork was broken in the installed PWA, and fine on the very first load
    before the worker activated — which is what made it look intermittent.
    """
    source = (JS_DIR / "sw.js").read_text()

    # Matched on the exact expressions rather than loose substrings — both
    # phrases also appear in the prose around them, and an earlier version of
    # this test happily found them in a comment.
    check = "url.origin !== self.location.origin"
    write = "cache.put(event.request"

    assert check in source, (
        "sw.js no longer compares the request's origin, so it is intercepting "
        "remote artwork again"
    )
    assert write in source, "sw.js's caching call moved; this guard needs updating"
    # The early return has to come before the caching path, or the check is
    # decoration.
    assert source.index(check) < source.index(write), (
        "the origin check must short-circuit before the cache write"
    )


def test_service_worker_api_prefixes_have_no_trailing_slash() -> None:
    """A prefix with a trailing slash (e.g. "/settings/") never matches the
    *bare* route ("/settings" has none of its own) — that exact bug shipped
    once already and meant GET /settings, GET /profiles and GET
    /recommendations were all getting cached instead of excluded, so a
    profile switch or creation could serve a stale, different profile's
    settings straight from the service worker cache. isApiPath's own
    path === prefix || path.startsWith(`${prefix}/`) check only works if the
    prefixes themselves stay bare."""
    source = (JS_DIR / "sw.js").read_text()

    match = re.search(r"const API_PREFIXES = \[(.*?)\];", source, re.DOTALL)
    assert match, "sw.js's API_PREFIXES list moved or was renamed; this guard needs updating"
    prefixes = re.findall(r'"([^"]+)"', match.group(1))
    assert prefixes, "found API_PREFIXES but no string entries in it"

    trailing_slash = [p for p in prefixes if p.endswith("/")]
    assert not trailing_slash, f"these API_PREFIXES entries have a trailing slash: {trailing_slash}"

    # The fetch handler has to actually call the boundary-aware matcher, not
    # a raw path.startsWith(prefix) loop over the (now-bare) prefixes —
    # otherwise a bare "/settings" would still slip past every prefix listed
    # here for the opposite reason (no prefix is a strict startsWith match of
    # an equal-length string).
    assert "isApiPath(url.pathname)" in source, (
        "sw.js's fetch handler no longer calls isApiPath — bare API routes "
        "can get cached again"
    )


def test_report_playback_only_sends_the_unexpected_events() -> None:
    """4 beacons were measured per track played under the old blanket policy
    — "now-playing", "play-requested" and a successful "playing" for
    starting a track, "track-ended" for finishing it — none of which are
    ever useful for debugging, since every track produces them whether or
    not anything went wrong. Pinned as an exact set rather than "at least
    these" so a future change to the allowlist is a deliberate edit here,
    not a silent one in player.js."""
    source = (JS_DIR / "player.js").read_text()

    match = re.search(r"const REPORTED_EVENTS = new Set\(\[(.*?)\]\);", source, re.S)
    assert match, "REPORTED_EVENTS allowlist not found in player.js"
    kept = {name.strip().strip('"') for name in match.group(1).split(",") if name.strip()}

    assert kept == {"play-rejected", "playback-stalled", "prepare-failed", "outgoing-ended"}

    # The allowlist alone proves nothing if reportPlayback doesn't actually
    # enforce it — this is the guard clause that turns "defined" into "used".
    # Checked as one contiguous block (not via _function_body's brace
    # matching) because reportPlayback's own `detail = {}` default parameter
    # contains a brace pair that helper isn't parameter-list-aware enough to
    # skip past.
    assert (
        "export function reportPlayback(event, detail = {}) {\n"
        "  if (!REPORTED_EVENTS.has(event)) return;"
    ) in source, (
        "REPORTED_EVENTS is defined but reportPlayback no longer checks "
        "against it — every event is being sent again"
    )


def test_the_volume_slider_is_gated_on_ios_as_well_as_on_the_write_taking() -> None:
    """iOS routes playback volume to the hardware buttons, so the slider does
    nothing there and is hidden. That used to be decided by feature detection
    alone — write a volume, read it back — on the reasoning that the
    restriction is per-browser rather than per-OS.

    That is measurably wrong on a modern iPhone, confirmed from the device
    (iOS 18.7, Safari 26.6): writing 0.5 and reading it back returns 0.5, so
    the detection reports "settable" over a slider that does nothing. Apple's
    documentation still claims reading always returns 1; it has stopped being
    true. Nothing readable separates the two cases any more, hence the second,
    user-agent gate — and hence this test, because "just feature-detect it" is
    exactly the tidy-looking change that would put the dead control back on
    every iPhone."""
    source = (JS_DIR / "player.js").read_text()

    assert "function isIOSWebKit()" in source, (
        "the iOS gate is gone — a volume slider that does nothing is back on every iPhone"
    )
    assert "volumeIsSettable(activeAudio())" in source, "the feature-detection gate is gone"
    assert "&& !isIOSWebKit()" in source, (
        "the volume slider is no longer gated on both the write taking *and* not being "
        "iOS; feature detection alone can show a dead control on an iPhone"
    )
    # iPadOS 13+ claims to be a Mac, so the sniff has to look past the name.
    assert "maxTouchPoints" in source, (
        "isIOSWebKit no longer distinguishes an iPad from a Mac, and iPadOS reports "
        "itself as Macintosh"
    )


def test_wire_scrollers_does_not_leak_a_listener_or_observer_per_row() -> None:
    """wireScrollers() runs again after every fragment swap (Home/Library
    rows get replaced wholesale), and it used to create a brand new
    ResizeObserver *and* a brand new `window` "mouseup" listener for every
    row, every single time — neither was ever torn down. Measured live: 5 of
    each at boot, 105 of each after 20 refreshes. The fix is structural
    (module-scope singletons, not per-row), so this checks the structure
    rather than actually leaking memory in a browser this suite can't run."""
    source = (JS_DIR / "home" / "scrollers.js").read_text()

    mouseup_registrations = source.count('addEventListener("mouseup"')
    assert mouseup_registrations == 1, (
        f"expected exactly one window mouseup listener, found {mouseup_registrations} "
        "— a per-row registration inside wireScrollers is back"
    )
    # The one registration that exists must be at module scope (outside
    # wireScrollers' body), or "exactly one" would just mean it moved rather
    # than stopped repeating.
    wire_scrollers_start = source.index("export function wireScrollers")
    assert source.index('addEventListener("mouseup"') < wire_scrollers_start, (
        "the mouseup listener is registered inside wireScrollers — it will "
        "run again, and leak again, on every fragment swap"
    )

    assert source.count("new ResizeObserver(") == 1, (
        "wireScrollers should create exactly one ResizeObserver kind of call "
        "site — if a per-swap observer is still made without being tracked "
        "for disconnection, the leak is back"
    )
    assert "observer.disconnect()" in source, (
        "no disconnect() call on the tracked observer — old rows' ResizeObservers "
        "are never torn down (a mention of .disconnect() in a comment doesn't count)"
    )


def test_downloads_modal_actions_refresh_its_own_list() -> None:
    """Clearing all downloads or removing one both run from *inside* the open
    Downloads modal — since refreshFragments() alone no longer touches that
    modal's list (see the sweep test above), those two actions have to opt
    back in explicitly, or a user's own action wouldn't appear to do
    anything until they closed and reopened the modal."""
    source = (JS_DIR / "home" / "settings.js").read_text()

    assert source.count("{ alsoDownloads: true }") == 2, (
        "expected exactly two confirmedAction calls (clear-storage, "
        "remove-download) to opt into refreshing the open modal's own list"
    )


def test_refresh_fragments_default_sweep_does_not_include_the_downloads_body() -> None:
    """/partials/downloads was 86.5KB — the Downloads modal's full item list —
    behind a modal that's closed the vast majority of the time, and
    refreshFragments() (called after every save/favorite/play) used to
    refetch it every single time regardless. See fragments.js's
    refreshDownloadsBody for where that list is fetched instead."""
    source = (JS_DIR / "fragments.js").read_text()
    fragments_block = source[source.index("const FRAGMENTS") : source.index("];") + 2]

    assert "downloads-body" not in fragments_block, (
        "FRAGMENTS' default sweep includes downloads-body again — the "
        "expensive modal list is back to being refetched on every action"
    )
    assert "refreshDownloadsBody" in source, (
        "the on-demand downloads-body refresh (called on #open-downloads and "
        "from settings.js's in-modal actions) is gone"
    )


def test_initial_tab_is_never_restored_from_local_storage() -> None:
    """Opening the app fresh starts on Home.

    The pre-paint script used to fall back to a localStorage copy of the last
    tab whenever the URL carried no hash — which is every PWA launch, every
    bookmark, and every reload done by profiles.js. Creating a profile from
    Settings → Manage profiles therefore handed the brand-new profile the
    Settings tab with the onboarding wizard sitting on top of it. The hash is
    still written on every tab switch (home/tabs.js), so a reload or a deep
    link keeps its tab; a fresh open with no hash is meant to mean Home.
    """
    index = Path("app/templates/index.html").read_text()
    tabs = (JS_DIR / "home" / "tabs.js").read_text()

    assert "spotea-active-tab" not in index, (
        "index.html's pre-paint script reads the remembered tab again — a "
        "fresh open no longer starts on Home"
    )
    assert "spotea-active-tab" not in tabs, "home/tabs.js is writing the remembered tab again"
    # The word itself still appears in the comment explaining why it's gone.
    assert "localStorage." not in tabs, "home/tabs.js is back to persisting the active tab"


def test_opening_explore_never_shows_a_loading_placeholder() -> None:
    """The shelves are fetched in the background at boot and re-checked
    quietly on every later visit. Entering the tab used to swap a spinner in
    over them — worst right after onboarding, where the interest list had
    just changed and the re-check was a full rebuild (several live YouTube
    searches) with the user watching it. An unchanged batch isn't re-rendered
    at all, since replacing every card with an identical copy flashes every
    thumbnail for nothing."""
    source = (JS_DIR / "home" / "explore.js").read_text()
    setup = _function_body(source, "setupRecommendations")

    activation = setup[setup.index("onTabActivated") :]
    assert "loadRecommendations()" in activation and "placeholder" not in activation, (
        "switching to Explore passes a placeholder again, so the tab is "
        "something you wait on rather than something you enter"
    )
    assert "renderedPayload" in source, (
        "the unchanged-batch check is gone — every re-check re-renders every "
        "shelf, and every <img> in it, identically"
    )

def test_an_artist_name_is_only_a_link_when_there_is_an_artist_to_open() -> None:
    """A song result carries the artist's channel id most of the time but not
    always — the fallback yt-dlp search (routers/explore.py's
    search_video_feeds) doesn't reliably report one on a flat entry. Rendering
    the name as a button anyway gives a control that opens nothing, which is
    worse than plain text."""
    source = (JS_DIR / "home" / "explore.js").read_text()

    body = source[source.index("function artistNameHtml(") :]
    body = body[: body.index("\n}\n")]

    assert "if (!name || !item.channel_id) return name;" in body, (
        "artistNameHtml no longer falls back to plain text for a result with "
        "no channel id — the name renders as a button that opens nothing"
    )


def test_the_artist_link_is_handled_before_the_card_it_sits_inside() -> None:
    """The name sits inside a .rec-card, and that whole card is a play
    target. Whichever branch runs first wins, so putting the artist check
    after the card's would make clicking the artist start the song."""
    source = (JS_DIR / "home" / "explore.js").read_text()

    listener = source[source.index('body.addEventListener("click"') :]
    listener = listener[: listener.index("\n  });")]

    assert listener.index('closest(".artist-link")') < listener.index('closest(".rec-card")'), (
        "the .rec-card branch is checked before .artist-link — clicking an "
        "artist's name plays the song instead of opening their page"
    )


def test_a_channel_result_opens_the_artist_route() -> None:
    """The fix for an artist whose YouTube channel is mostly vlogs: the
    search result and the shelf card both go to yt-artist, and the server
    decides whether that id is an artist or falls back to the channel's
    uploads (see services/remote_detail.py). Sending them to yt-channel
    instead skips that decision and shows the vlogs again."""
    source = (JS_DIR / "home" / "explore.js").read_text()

    assert 'openDetail("yt-channel"' not in source, (
        "a channel result opens yt-channel directly again — an artist's "
        "track list is never reached, only their uploads"
    )
    assert source.count('openDetail("yt-artist", channelCard.dataset.channelId') == 1
    assert source.count('openDetail("yt-artist", row.dataset.channelId') == 1


def test_the_scroller_module_has_no_import_cycle() -> None:
    """Drag-to-scroll moved out of home/library.js so home/detail.js could
    wire the artist profile's shelves after a panel swap. Importing it back
    from library.js would recreate the cycle the move exists to avoid —
    library.js already imports openDetail from detail.js."""
    detail = (JS_DIR / "home" / "detail.js").read_text()
    scrollers = (JS_DIR / "home" / "scrollers.js").read_text()

    assert 'from "./scrollers.js"' in detail, "detail.js no longer wires the profile's shelves"
    assert 'from "./library.js"' not in detail, (
        "detail.js imports library.js, which imports detail.js — an import cycle"
    )
    assert not [line for line in scrollers.split("\n") if line.startswith("import ")], (
        "the scroller module took on a dependency — it is meant to be a leaf"
    )


def test_both_panel_swaps_run_the_same_wiring() -> None:
    """A cached fragment and a freshly fetched one are the same markup, so
    anything one needs wired the other does too. They used to diverge."""
    source = (JS_DIR / "home" / "detail.js").read_text()

    assert source.count("  afterPanelSwap();") == 2, (
        "a swap path skips afterPanelSwap — its shelves won't drag-scroll, "
        "or its shuffle button won't match the current preference"
    )


def test_a_release_card_opens_by_browse_id() -> None:
    """An album carries an audioPlaylistId and a single doesn't, so the
    browse id is the only identifier that works for both — see
    music.ArtistRelease."""
    source = (JS_DIR / "home" / "detail.js").read_text()

    assert 'openDetail("yt-release", releaseCard.dataset.releaseId)' in source


def test_a_single_is_resolved_before_any_history_is_pushed() -> None:
    """A one-track release plays instead of opening a panel (see
    routers/partials.py's remote_release_fragment), so it must not leave a
    history entry pointing at a panel that was never shown. The only way to
    guarantee that is to ask what the release is before pushing."""
    source = (JS_DIR / "home" / "detail.js").read_text()

    resolve = source.index("await resolveRelease(id)")
    push = source.index("history.pushState")
    assert resolve < push, "resolveRelease must run before openDetail pushes history"


def test_playing_a_standalone_track_sets_a_one_track_queue() -> None:
    """Nothing playRemoteVideo opens came out of a list, so there is no rest
    of it to queue. Left to itself, queue.js's noteCurrent would clear the
    queue instead and the queue panel would go blank — which on desktop is a
    permanently open panel showing nothing while a track plays."""
    source = (JS_DIR / "home" / "remote.js").read_text()

    play = source.index("export async function playRemoteVideo")
    set_queue = source.index('setQueue({ kind: "single" }, [data.content_id])', play)
    open_player = source.index("openPlayer(data.content_id)", play)
    # Order matters: noteCurrent runs off openPlayer and drops any queue the
    # new track isn't in, so the queue has to exist first.
    assert set_queue < open_player


def test_a_new_follow_lands_on_the_artists_profile() -> None:
    """The landing page has to match the one the library card opens for the
    very same artist — following someone and clicking them a minute later
    must not show two different pages."""
    source = (JS_DIR / "home" / "detail.js").read_text()

    assert 'openDetail("yt-artist", event.detail.browseId)' in source


def test_the_follow_event_carries_what_the_server_decided() -> None:
    """Off the response, not off the request: the caller sends a channel URL
    and the server says whose page that turned out to be."""
    source = (JS_DIR / "home" / "remote.js").read_text()

    assert "browseId: data.artist.browse_id || null" in source


def test_the_first_sync_counts_as_an_artist_still_filling_in() -> None:
    """"syncing" is the only phase there is: the server puts an artist in it
    before fetching anything, and Library's card polls on exactly that."""
    initial_sync = Path("app/services/initial_sync.py").read_text()
    library = (JS_DIR / "home" / "library.js").read_text()

    assert 'ACTIVE_PHASES = frozenset({"syncing"})' in initial_sync
    assert "/artists/syncing" in library


def test_every_module_import_resolves() -> None:
    """The regression this exists for: a rename left home/remote.js exporting
    neither playRemoteVideo nor playRemoteList while home/explore.js and
    home/detail.js still imported both. One unresolved import fails the whole
    module graph, so *nothing* on the page was wired — no tabs, no menu, no
    play button — and every server-side test still passed.

    There is no JS runner here to catch that by executing it, so this parses
    the import/export graph instead: every named import has to be exported by
    the file it names, and that file has to exist.
    """
    modules = {path.resolve(): path.read_text() for path in JS_DIR.rglob("*.js")}

    exported = {}
    for path, source in modules.items():
        names = set(re.findall(r"^export (?:async )?function (\w+)", source, re.M))
        names |= set(re.findall(r"^export (?:const|let|class) (\w+)", source, re.M))
        exported[path] = names

    broken = []
    for path, source in modules.items():
        for match in re.finditer(r'import\s*\{([^}]+)\}\s*from\s*"([^"]+)"', source):
            target = (path.parent / match.group(2)).resolve()
            if target not in modules:
                broken.append(f"{path.name} imports a file that doesn't exist: {match.group(2)}")
                continue
            for name in (n.strip().split(" as ")[0] for n in match.group(1).split(",") if n.strip()):
                if name not in exported[target]:
                    broken.append(f"{path.name} imports {name}, which {target.name} does not export")

    assert not broken, "\n".join(broken)


def test_the_lyrics_panel_only_listens_to_the_audio_element() -> None:
    """Following along is a timeupdate consumer and nothing more. The
    player's iOS behaviour is load-bearing and easy to break from outside —
    an unrelated module calling pause() is exactly the bug that cost days
    (see player.js's notes on background playback)."""
    source = (JS_DIR / "home" / "lyrics.js").read_text()

    for forbidden in (".play()", ".pause()", ".src =", ".load()", "new Audio"):
        assert forbidden not in source, f"lyrics.js touches the audio element: {forbidden}"
    assert 'onPlayerEvent("timeupdate"' in source


def test_lyrics_are_not_fetched_until_the_tab_is_opened() -> None:
    """A miss is two live YouTube requests and most tracks have none, so
    fetching on play would spend the request budget on answers nobody asked
    for. The only paths that may call load() are selecting the tab and, once
    selected, the track changing underneath it."""
    source = (JS_DIR / "home" / "lyrics.js").read_text()

    # Every call site of load() sits after a check that the lyrics tab is the
    # selected one.
    for match in re.finditer(r"^\s*(?:else )?(?:if \([^)]*\) )?load\(", source, re.M):
        before = source[: match.start()]
        assert 'selected !== "lyrics"' in before or "isLyrics" in before, (
            "load() is reachable without the Lyrics tab being selected"
        )


def test_the_pinned_panel_breakpoint_matches_the_stylesheet() -> None:
    """The panel counts as open on a wide screen so the existing load and
    refresh paths keep working (see overlay.js's setQueueOpen). If the two
    breakpoints drift, the panel is either invisible-but-loading or
    visible-but-empty."""
    overlay = (JS_DIR / "home" / "overlay.js").read_text()
    css = (JS_DIR.parent / "css" / "style.css").read_text()

    assert 'matchMedia("(min-width: 900px)")' in overlay
    assert "@media (min-width: 900px)" in css


def test_moods_is_the_first_shelf_in_explore() -> None:
    """It is the only shelf that needs nothing followed and nothing typed,
    so on a library that has just been created it is the difference between
    Explore being somewhere to start and Explore being a heading over
    nothing."""
    source = (JS_DIR / "home" / "explore.js").read_text()
    shelves = source[source.index("const shelves = ["): source.index("].join(\"\")")]

    calls = re.findall(r"(moodsShelfHtml|shelfHtml)\(", shelves)
    assert calls[0] == "moodsShelfHtml", f"Moods is not first — order is {calls}"


def test_the_moods_shelf_does_not_promise_genres() -> None:
    """ytmusicapi fails to parse 25 of YouTube Music's 40 mood categories and
    they are every entry under Genres (see music.MOOD_SECTION), so only the
    moods section is listed. The old "Moods & genres" heading offered Rock
    and Jazz and then showed neither."""
    source = (JS_DIR / "home" / "explore.js").read_text()

    assert "Moods &amp; genres" not in source
    assert '<h3 class="shelf-title">Moods</h3>' in source
