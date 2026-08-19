"""The interests list itself: normalization (app/interests.py) and the
Settings endpoints that read and write it.

Recommendations built *from* an interests list live in
test_recommendations.py — nothing here goes near YouTube.
"""

import pytest

from app.interests import (
    MAX_INTEREST_LENGTH,
    MAX_INTERESTS,
    interests_signature,
    normalize_interests,
    parse_interests,
    serialize_interests,
)
from app.models import User

USER_ID = 1


@pytest.fixture(autouse=True)
def _reset_interests(db_session):
    """The default profile survives conftest's between-test cleanup (it only
    deletes rows), so an interests list written by one test would otherwise
    still be there for the next one."""
    yield
    profile = db_session.get(User, USER_ID)
    profile.interests = None
    db_session.commit()


def test_blank_and_whitespace_only_entries_are_dropped():
    assert normalize_interests(["jazz", "  ", "", "\t\n"]) == ["jazz"]


def test_inner_whitespace_is_collapsed():
    # Also what guarantees no tag can contain the newline the column is
    # split on.
    assert normalize_interests(["  türk   rock\n\nsomething "]) == ["türk rock something"]


def test_duplicates_are_dropped_case_insensitively_keeping_the_first():
    assert normalize_interests(["Jazz", "JAZZ", "jazz "]) == ["Jazz"]


def test_order_is_preserved():
    assert normalize_interests(["c", "a", "b"]) == ["c", "a", "b"]


def test_over_long_tags_are_truncated_not_rejected():
    (tag,) = normalize_interests(["x" * (MAX_INTEREST_LENGTH + 50)])
    assert tag == "x" * MAX_INTEREST_LENGTH


def test_the_list_is_capped():
    assert len(normalize_interests([f"tag {i}" for i in range(MAX_INTERESTS + 10)])) == MAX_INTERESTS


def test_serialize_parse_round_trip():
    assert parse_interests(serialize_interests(["jazz", "türk rock"])) == ["jazz", "türk rock"]


@pytest.mark.parametrize("raw", [None, "", "\n\n  \n"])
def test_an_unset_column_parses_as_no_interests(raw):
    assert parse_interests(raw) == []


def test_signature_ignores_order_and_case():
    assert interests_signature(["Jazz", "türk rock"]) == interests_signature(["TÜRK ROCK", "jazz"])


def test_signature_changes_when_an_interest_is_added():
    assert interests_signature(["jazz"]) != interests_signature(["jazz", "funk"])


def test_settings_reports_no_interests_by_default(client):
    assert client.get("/settings").json()["interests"] == []


def test_put_stores_interests_and_reports_them_back(client):
    res = client.put("/settings", json={"interests": ["jazz", "türk rock"]})
    assert res.status_code == 200
    assert res.json()["interests"] == ["jazz", "türk rock"]
    assert client.get("/settings").json()["interests"] == ["jazz", "türk rock"]


def test_put_normalizes_rather_than_rejecting(client):
    # An interest is free text — there's nothing to 400 on, so a messy list
    # comes back cleaned up instead.
    res = client.put("/settings", json={"interests": ["  jazz ", "JAZZ", ""]})
    assert res.status_code == 200
    assert res.json()["interests"] == ["jazz"]


def test_put_replaces_the_whole_list(client):
    client.put("/settings", json={"interests": ["jazz", "funk"]})
    assert client.put("/settings", json={"interests": ["funk"]}).json()["interests"] == ["funk"]


def test_an_empty_list_clears_interests(client):
    client.put("/settings", json={"interests": ["jazz"]})
    assert client.put("/settings", json={"interests": []}).json()["interests"] == []


def test_omitting_interests_leaves_them_alone(client):
    client.put("/settings", json={"interests": ["jazz"]})
    # The audio-quality control PUTs only its own field — it must not wipe
    # the list the interests editor manages.
    res = client.put("/settings", json={"audio_quality": "low"})
    assert res.json() == {
        "audio_quality": "low",
        "refresh_interval_minutes": 30,
        "interests": ["jazz"],
    }


def test_interest_chips_are_rendered_into_the_settings_panel(client):
    client.put("/settings", json={"interests": ["türk rock"]})
    # Handed to home/settings.js as JSON on the element, not as markup — see
    # index.html.
    assert "t\\u00fcrk rock" in client.get("/").text
