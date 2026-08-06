import pytest

from app.formatting import format_duration, format_size, safe_filename


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (None, ""),
        (0, "0:00"),
        (5, "0:05"),
        (59, "0:59"),
        (60, "1:00"),
        (125, "2:05"),
        (3599, "59:59"),
        (3600, "1:00:00"),
        (3661, "1:01:01"),
    ],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


@pytest.mark.parametrize(
    "num_bytes, expected",
    [
        (None, "0 MB"),
        (0, "0 MB"),
        (1024 * 1024, "1.0 MB"),
        (10 * 1024 * 1024, "10.0 MB"),
        (1536 * 1024 * 1024, "1.50 GB"),
    ],
)
def test_format_size(num_bytes, expected):
    assert format_size(num_bytes) == expected


def test_safe_filename_strips_illegal_characters():
    assert safe_filename('a/b\\c:d*e?f"g<h>i|j') == "abcdefghij"


def test_safe_filename_collapses_whitespace_and_trims_dots():
    assert safe_filename("  My   Video...  ") == "My Video"


def test_safe_filename_falls_back_when_nothing_survives():
    assert safe_filename("///???***") == "download"


def test_safe_filename_truncates_long_titles():
    result = safe_filename("x" * 300)
    assert len(result) == 150


def test_safe_filename_keeps_normal_titles_unchanged():
    assert safe_filename("Somewhat Optimistic About AI Future") == "Somewhat Optimistic About AI Future"
