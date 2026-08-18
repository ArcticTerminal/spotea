"""Predefined chip lists for the onboarding wizard's genre/category step (see
home/onboarding.js) — the single lists both the template and each seeding
pipeline key off of, so a hardcoded HTML list and a hardcoded Python list
can't drift apart.

Suggested channels per entry come from hand-curated seed scripts
(scripts/seed_music_artists.py, scripts/seed_podcast_channels.py) into the
genre_artists cache — MusicBrainz tag search (scripts/seed_genre_artists.py,
kept as the historical pipeline) surfaced arbitrary tagged artists rather
than famous ones, and podcasts have no MusicBrainz equivalent at all. A
genre added here without a matching curated seed entry still works as a
chip; it just contributes no suggestions.

PODCAST_CATEGORIES is the Apple Podcasts / Spotify top-level category list
(the same taxonomy real podcast RSS feeds declare via <itunes:category>) —
kept as the industry-standard set rather than invented from scratch, both
for user-facing familiarity and in case a podcast's own declared category
is ever usable as a signal later.
"""

MUSIC_GENRES = [
    "Pop",
    "Rock",
    "Hip-Hop",
    "R&B",
    "Electronic",
    "Jazz",
    "Classical",
    "Metal",
    "Indie",
    "Lo-fi",
    "K-Pop",
    "Latin",
    "Country",
    "Reggae",
    "Folk",
    "Punk",
    "Soul",
    "World",
    "House",
    "Techno",
    "Trance",
    "Dubstep",
    "Drum & Bass",
    "Ambient",
    "Funk",
    "Blues",
    "Gospel",
    "Disco",
    "Grunge",
    "Hard Rock",
    "Emo",
    "Ska",
    "Afrobeat",
    "J-Pop",
    "Salsa",
    "Singer-Songwriter",
    "Trap",
    "New Age",
]

PODCAST_CATEGORIES = [
    "Arts",
    "Business",
    "Comedy",
    "Education",
    "Fiction",
    "Government",
    "Health & Fitness",
    "History",
    "Kids & Family",
    "Leisure",
    "Music",
    "News",
    "Religion & Spirituality",
    "Science",
    "Society & Culture",
    "Sports",
    "Technology",
    "True Crime",
    "TV & Film",
]
