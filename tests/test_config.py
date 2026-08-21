"""Settings that need more than reading an env var straight through.

Only chart_countries so far: it parses a list, has a deprecated predecessor
to keep honouring, and has to be impossible to reduce to nothing.
"""

import pytest

from app.config import DEFAULT_CHART_COUNTRIES, Settings

DEFAULT = DEFAULT_CHART_COUNTRIES.split(",")


def _settings(**overrides):
    """A Settings built from arguments alone. _env_file=None so the repo's
    own .env can't leak into these — the point is what the code does with a
    given value, not what this machine happens to be configured with."""
    return Settings(secret_key="test", _env_file=None, **overrides)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("ZZ", ["ZZ"]),
        ("TR", ["TR"]),
        ("TR,US,GB,DE", ["TR", "US", "GB", "DE"]),
        # Whitespace and case are what someone actually types.
        (" tr , us ", ["TR", "US"]),
        # Never empty: an empty setting means "the default", not "no charts".
        ("", DEFAULT),
        (",,", DEFAULT),
    ],
)
def test_chart_countries_parses_a_list(configured, expected):
    assert _settings(music_chart_countries=configured).chart_countries == expected


def test_the_old_single_country_setting_still_works():
    """MUSIC_CHART_COUNTRY was the setting before several were allowed.
    pydantic's extra="ignore" would drop it silently on upgrade and hand the
    user the global chart without a word, which is a worse outcome than any
    amount of deprecation code."""
    assert _settings(music_chart_country="TR").chart_countries == ["TR"]


def test_the_new_setting_wins_when_both_are_present():
    """Otherwise a forgotten line in an old .env quietly overrides the one
    the user just edited."""
    settings = _settings(music_chart_country="TR", music_chart_countries="US,GB")

    assert settings.chart_countries == ["US", "GB"]


def test_the_default_is_the_english_speaking_markets():
    """A choice about what this app is for rather than a technical one. The
    global chart is still reachable by asking for "ZZ"."""
    assert _settings().chart_countries == ["US", "GB", "CA", "AU", "IE", "NZ"]
    assert _settings(music_chart_countries="ZZ").chart_countries == ["ZZ"]


def test_the_deprecated_setting_is_not_pinned_to_a_country_code():
    """The check for "has the new setting been touched?" used to compare
    against the literal "ZZ", which was the default at the time. That reads
    as a working deprecation shim right up until the default changes, at
    which point MUSIC_CHART_COUNTRY is silently ignored — no error, just the
    wrong charts. Pinned here so the next default change cannot repeat it."""
    assert _settings(music_chart_country="TR").chart_countries == ["TR"]
    assert "ZZ" not in DEFAULT_CHART_COUNTRIES
