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


def test_setup_profiles_does_not_eagerly_fetch_the_profile_list() -> None:
    """GET /profiles used to run on every single page load to fill two
    overlays that start hidden and, on a typical visit, are never opened —
    see home/profiles.js's ensureProfilesLoaded. This pins the regression: a
    call to loadProfiles() directly inside setupProfiles (rather than behind
    ensureProfilesLoaded, deferred until an overlay actually opens) would
    silently restore the eager fetch."""
    source = _function_body(
        (JS_DIR / "home" / "profiles.js").read_text(), "setupProfiles"
    )

    assert "loadProfiles()" not in source, (
        "setupProfiles calls loadProfiles() directly — the boot-time fetch is back"
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

    match = re.search(r'const REPORTED_EVENTS = new Set\(\[(.*?)\]\);', source)
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


def test_explore_recommendations_split_by_duration_at_ten_minutes() -> None:
    """Measured live: Music profile 11/11 recommended videos ran over 10
    minutes; Podcast 10/12, median 2:18:19. Splitting onto a "Long form"
    shelf instead of filtering it out matters specifically for the Podcast
    profile — its own interests (linux, devops) make hour-long content the
    *correct* result, not noise."""
    source = (JS_DIR / "home" / "explore.js").read_text()

    assert "const LONG_FORM_THRESHOLD_SECONDS = 10 * 60;" in source
    assert 'shelfHtml("Contents", contents, recVideoCardHtml)' in source
    assert 'shelfHtml("Long form", longForm, recVideoCardHtml)' in source


def test_wire_scrollers_does_not_leak_a_listener_or_observer_per_row() -> None:
    """wireScrollers() runs again after every fragment swap (Home/Library
    rows get replaced wholesale), and it used to create a brand new
    ResizeObserver *and* a brand new `window` "mouseup" listener for every
    row, every single time — neither was ever torn down. Measured live: 5 of
    each at boot, 105 of each after 20 refreshes. The fix is structural
    (module-scope singletons, not per-row), so this checks the structure
    rather than actually leaking memory in a browser this suite can't run."""
    source = (JS_DIR / "home" / "library.js").read_text()

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


def test_profile_changes_reload_onto_home() -> None:
    """Switching, creating, or deleting the current profile all reload, and
    all of them have to land on Home: every panel is profile-scoped, and a
    #channel/42 hash names a feed the incoming profile doesn't have."""
    source = (JS_DIR / "home" / "profiles.js").read_text()
    helper = _function_body(source.replace("function reloadAtHome", "export function reloadAtHome"), "reloadAtHome")

    assert 'replaceState(null, "", "/#home")' in helper, (
        "reloadAtHome no longer rewrites the URL before reloading — assigning "
        "a URL that differs only in its fragment reloads nothing"
    )
    assert source.count("window.location.reload()") == 1, (
        "a profile change reloads without going through reloadAtHome, so it "
        "keeps whatever tab or detail hash the outgoing profile had"
    )


def test_onboarding_wizard_has_no_way_out_but_finishing() -> None:
    """It's a required step: a profile that dismisses it is left with the
    empty library and empty "For you" shelves it exists to prevent, and since
    needs_onboarding goes false the moment one channel exists, one add was
    enough to never be offered it again."""
    index = Path("app/templates/index.html").read_text()
    onboarding = (JS_DIR / "home" / "onboarding.js").read_text()

    assert 'id="onboarding-close"' not in index, "the wizard has a close button again"
    assert "{ dismissible: false }" in onboarding, (
        "the wizard's overlay is dismissible again — backdrop clicks and "
        "Escape close it"
    )
    assert "const REQUIRED_CHANNELS" in onboarding, "the channel gate on Finish is gone"


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


def test_onboarding_add_reports_before_it_waits() -> None:
    """"Add" says "Added" on the press, not after the round trip.

    Adding a channel is an RSS sync plus a yt-dlp history backfill. Held
    behind that, picking six channels was six waits stacked on each other,
    and pressing Finish early meant watching the app redraw itself. The click
    handler is therefore not async: it labels the row, counts it, and queues
    the work.
    """
    source = (JS_DIR / "home" / "onboarding.js").read_text()

    assert 'container.addEventListener("click", (event) => {' in source, (
        "the channels step's click handler is async again — the label is back "
        "to waiting on the request"
    )
    assert 'btn.textContent = "Added";' in source, "the optimistic label is gone"
    assert "enqueue(" in source, "the add queue is gone"


def test_onboarding_never_waits_for_a_history_scan() -> None:
    """POST /feeds resolves the channel and applies its RSS feed before it
    answers, so the channel's recent uploads are in the library at that
    point. The one-time full-history scan behind it runs server-side and is
    minutes long on a big channel (6,504 videos for one in the author's own
    library) — the wizard used to hold a loading screen for it, for content
    nothing on the first screen needs. Library's own card carries that wait
    now."""
    onboarding = (JS_DIR / "home" / "onboarding.js").read_text()
    library = (JS_DIR / "home" / "library.js").read_text()
    index = Path("app/templates/index.html").read_text()

    assert "waitForHistory: false" in onboarding, (
        "the wizard is waiting for channel history again"
    )
    assert "onboarding-step-preparing" not in index, (
        "the wizard's wait-for-backfill screen is back"
    )
    assert "/feeds/backfilling" in library, (
        "Library no longer checks which channels are still being fetched, so "
        "its 'Fetching uploads…' cards can never turn back into counts"
    )


def test_onboarding_adds_run_one_at_a_time() -> None:
    """Each add resolves a channel against a service that rate-limits an
    unauthenticated residential IP, so the queue drains one job at a time —
    the same thing the server's own bulk importer does. Five in flight at
    once is the burst this exists to avoid."""
    source = (JS_DIR / "home" / "onboarding.js").read_text()

    assert source.count("await followChannel(") == 1, (
        "more than one followChannel await in the wizard — the queue is no "
        "longer the single path adds go through"
    )
    assert 'jobs.find((candidate) => candidate.status === "queued")' in source, (
        "the queue no longer walks jobs one at a time"
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
