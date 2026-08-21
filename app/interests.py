"""A profile's interests — the free-text tags Settings collects, and the only
input Explore's recommendations are built from (see
services/recommendations.py).

Stored as one newline-separated string on `users.interests` rather than in a
table of its own: nothing ever queries a single interest in SQL. The
recommendation builder reads the whole list, hands each entry to YouTube
search verbatim, and that's the end of it — a join table would buy ordering
and nothing else.
"""

import hashlib
from collections.abc import Iterable

# Both caps exist to bound what a single recommendation run can be asked to
# do (see services/recommendations.py, which samples from this list) and to
# keep one profile's row from growing without limit. Neither is a value a
# real user is likely to hit.
MAX_INTERESTS = 20
MAX_INTEREST_LENGTH = 60


def normalize_interests(values: Iterable[str]) -> list[str]:
    """Trims each tag, collapses inner whitespace, drops blanks, and removes
    case-insensitive duplicates while keeping the order they were added in.

    Whitespace collapsing isn't cosmetic: it's what guarantees no tag can
    contain a newline, which is what makes the newline-separated storage
    format above safe to split back apart.
    """
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        tag = " ".join(str(value).split())[:MAX_INTEREST_LENGTH].strip()
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(tag)
        if len(result) == MAX_INTERESTS:
            break
    return result


def parse_interests(raw: str | None) -> list[str]:
    """The stored column back as a list. Normalizes on the way out too, so a
    row written before a cap changed still comes back within it."""
    return normalize_interests((raw or "").splitlines())


def serialize_interests(values: Iterable[str]) -> str:
    return "\n".join(normalize_interests(values))


def interests_signature(values: Iterable[str]) -> str:
    """Stable id for one exact set of interests — what tells a cached
    recommendation batch apart from one built before the user edited the list
    (see services/recommendations.py).

    Order-insensitive and case-insensitive: reordering or recapitalizing the
    same tags would produce the same searches, so neither should throw away a
    perfectly good cached batch.
    """
    joined = "\n".join(sorted(tag.casefold() for tag in normalize_interests(values)))
    return hashlib.sha256(joined.encode()).hexdigest()[:32]


# What first-run onboarding offers (see templates/_home_shelves.html). A
# static list because there is no live one to use: YouTube Music publishes a
# "Genres" browse section, but ytmusicapi fails to parse 25 of its 40
# categories and those 25 are every single genre in it (see
# youtube/music.MOOD_SECTION, where the same limitation already forces the
# moods-only shelf).
#
# Suggestions, not a vocabulary. These land in exactly the same free-text
# field Settings writes, get handed to YouTube search verbatim like anything
# typed there, and nothing anywhere validates against this list — it exists
# so that a brand new library has something to click instead of a text box
# and no idea what belongs in it.
#
# Under MAX_INTERESTS deliberately: someone who picks every one of these
# should still have room to add their own afterwards.
SUGGESTED_GENRES = (
    "Pop",
    "Rock",
    "Hip-Hop",
    "R&B",
    "Electronic",
    "House",
    "Jazz",
    "Soul",
    "Funk",
    "Blues",
    "Metal",
    "Indie",
    "Classical",
    "Country",
    "Reggae",
    "Latin",
    "Folk",
    "Afrobeats",
)


def interest_chips(interests: Iterable[str]) -> list[tuple[str, bool]]:
    """The chips the picker draws: (label, is selected).

    The fixed list above, followed by anything already saved that isn't in
    it. That tail is the reason this function exists rather than the template
    looping over SUGGESTED_GENRES directly. Settings used to edit interests
    as free text, so a real library can hold tags the list has never heard of
    — the live one holds "rap", which is not a suggested genre — and drawing
    only the fixed list would have made those invisible and, worse,
    unremovable: the picker saves exactly the chips it shows, so an interest
    with no chip would be silently dropped the first time anything was
    toggled.

    Matched case-insensitively, and the suggested spelling wins where both
    exist: a stored "hip-hop" lights up the "Hip-Hop" chip rather than
    appearing again beside it.
    """
    saved = {tag.casefold(): tag for tag in normalize_interests(interests)}
    chips = [(genre, genre.casefold() in saved) for genre in SUGGESTED_GENRES]
    suggested = {genre.casefold() for genre in SUGGESTED_GENRES}
    chips.extend((tag, True) for key, tag in saved.items() if key not in suggested)
    return chips
