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
