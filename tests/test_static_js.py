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
