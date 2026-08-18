"""Hand-curated podcast-channel seed for the onboarding wizard's category
step — the podcast-side counterpart of scripts/seed_genre_artists.py.

Podcast categories deliberately reuse the genre_artists table (see
app/models.py's GenreArtist): the serving endpoint matches a profile's
interest strings against GenreArtist.genre, so seeding rows whose genre is a
PODCAST_CATEGORIES entry makes /onboarding/suggested-channels work for
podcast categories with zero backend changes. Only genre/artist_name/
channel_id/channel_url are written here — display metadata (title, avatar,
subscriber count) is resolved lazily by get_suggested_channels the first
time a real onboarding session needs the category, exactly like the music
rows.

Unlike MUSIC_GENRES there is no automated source to seed from — MusicBrainz
has no podcast equivalent — so CURATED_CHANNELS below is maintained by hand:
famous, currently-active shows per category that publish full episodes on an
official YouTube channel, each channel id resolved from the channel's own
page (curated 2026-08-18).

Run from the repo root:
    .venv/bin/python -m scripts.seed_podcast_channels

Safe to re-run: rows are keyed (genre, channel_id) and existing ones are
skipped, so this only ever does new work (a show added to CURATED_CHANNELS
since the last run).
"""

from app.database import SessionLocal
from app.genres import PODCAST_CATEGORIES
from app.models import GenreArtist
from app.services.genre_artists import build_row
from app.timeutil import utcnow
from scripts.channel_profiles import PROFILES

# One entry per PODCAST_CATEGORIES value, spelled exactly as there — the
# assertion in main() keeps the two from drifting apart. Each show is
# (display name, YouTube channel id); the name doubles as GenreArtist's
# artist_name, which _resolve falls back to if YouTube ever stops serving
# the channel.
CURATED_CHANNELS: dict[str, list[tuple[str, str]]] = {
    "Arts": [
        ("99% Invisible", "UCVMF2HD4ZgC0QHpU9Yq5Xrw"),
        ("The Moth", "UCxw-YwaBR9lVa1fmreWy94g"),
        ("Off Menu with Ed Gamble and James Acaster", "UCgFAyHxA0MBioGICaiU6amA"),
        ("Dish", "UCPHzAcg5sT62cO5hgAeTpVg"),
        ("Table Manners with Jessie and Lennie Ware", "UCDD0oCEBu0yKT3zhXI7gmZQ"),
        ("The Adam Buxton Podcast", "UC8bvbjqjxdLfWbq8UT3zBaA"),
        ("Fashion Neurosis with Bella Freud", "UCAIpmzc5XckHqBpIQKumKPg"),
    ],
    "Business": [
        ("The Diary of a CEO", "UCGq-a57w-aPwyi3pW7XLiHw"),
        ("All-In Podcast", "UCESLZhusAkFfsNsApnjF_Cg"),
        ("My First Million", "UCyaN6mg5u8Cjy2ZI4ikWaug"),
        ("The Ramsey Show", "UC7eBNeDW1GQf2NJQ6G6gAxw"),
        ("Acquired", "UCyFqFYfTW2VoIQKylJ04Rtw"),
        ("The Prof G Pod", "UC1E1SVcVyU3ntWMSQEp38Yw"),
        ("The GaryVee Audio Experience", "UCctXZhXmG-kf3tlIXgVZUlw"),
        ("The Iced Coffee Hour", "UCg0cxj8IR-2raYSGy_cq9bw"),
        ("BiggerPockets Real Estate Podcast", "UCVWDbXqQ8cupuVpotWNt2eg"),
        ("The Compound and Friends", "UCBRpqrzuuqE8TZcWw75JSdw"),
        ("The Money Guy Show", "UC9vUu4vlIlMC0dHQCTvQPbg"),
    ],
    "Comedy": [
        ("SmartLess", "UC-W8Qu2Zb407kjVhW-5U-SA"),
        ("This Past Weekend w/ Theo Von", "UC5AQEUAwCh1sGDvkQtkDWUQ"),
        ("Bad Friends", "UCRBpynZV0b7ww2XMCfC17qg"),
        ("Kill Tony", "UCwzCMiicL-hBUzyjWiJaseg"),
        ("Your Mom's House", "UCYIgiXwJck_Pb5Nj-wIrsqg"),
        ("Conan O'Brien Needs a Friend", "UCi7GJNg51C3jgmYTUwqoUXA"),
        ("Flagrant", "UC5PstSsGrRwj2o6asQpC4Rg"),
        ("Are You Garbage?", "UCaPOHjHoCbbnxZLHu9wgRRw"),
        ("Distractible", "UCUEUEcxMUWAw1w2BPgwe2qQ"),
        ("The Basement Yard", "UC_a6c3KLo9reMOqn2pbvMqg"),
    ],
    "Education": [
        ("The Mel Robbins Podcast", "UCk2U-Oqn7RXf-ydPqfSxG5g"),
        ("TED Talks Daily", "UCAuUUnT6oDeKwE6v1NGQxug"),
        ("The Jordan B. Peterson Podcast", "UCL_f53ZEJxp8TtlOkHwMV9Q"),
        ("Modern Wisdom", "UCIaH-gZIVC432YRjNVvnyCA"),
        ("The School of Greatness", "UCKsP3v2JeT2hWI_HzkxWiMA"),
        ("On Purpose with Jay Shetty", "UCbV60AGIHKz2xIGvbk0LLvg"),
        ("The Knowledge Project", "UCLtTf_uKt0Itd0NG7txrwXA"),
        ("The Tim Ferriss Show", "UCznv7Vf9nBdJYvBagFdAHWw"),
        ("6 Minute English (BBC Learning English)", "UCHaHD477h-FeBbVh9Sh7syA"),
    ],
    "Fiction": [
        ("Welcome to Night Vale", "UCrvuY59InDI3iKvopKT8PEw"),
        ("The Magnus Archives (Rusty Quill)", "UCAfn5ahWLpQmCIN7D0E1YQg"),
        ("CreepCast", "UC8Gwis8sr3O-IVA7BGDxh9g"),
        ("The NoSleep Podcast", "UC7iToOiqbE9QRVyTE73GPgg"),
        ("We're Alive", "UCIShWN6mnXUm9HXbZ_wJqNA"),
        ("Old Gods of Appalachia", "UC0HevYbkZ1_T86rjbzspefQ"),
        ("The White Vault", "UCPaOxWK6Wau96cfG0cCwxQA"),
        ("Chilling Tales for Dark Nights", "UC79H1bXWDNodOD8_VtZd_DA"),
    ],
    "Government": [
        ("The Lawfare Podcast", "UCh_9jgYqQNYlk8daCZFRD1A"),
        ("Strict Scrutiny", "UCk-Km4tcqAbhpnbrvj1pJFw"),
        ("Pod Save America", "UCKRoXz3hHAu2XL_k3Ef4vJQ"),
        ("The Bulwark Podcast", "UCG4Hp1KbGw4e02N7FpPXDgQ"),
        ("The President's Daily Brief", "UCbWraa1DoXrFwX3oK1zattQ"),
        ("The Rest Is Politics", "UCsufaClk5if2RGqABb-09Uw"),
        ("The Ezra Klein Show", "UCnxuOd8obvLLtf5_-YKFbiQ"),
    ],
    "Health & Fitness": [
        ("Huberman Lab", "UC2D2CMWXMOVWx7giW1n3LIg"),
        ("The Peter Attia Drive", "UC8kGsMa0LygSX9nkBcBH1Sg"),
        ("The Rich Roll Podcast", "UCpjlh0e319ksmoOD7bQFSiw"),
        ("FoundMyFitness", "UCWF8SqJVNlx-ctXbLswcTcA"),
        ("Mind Pump", "UCq0hKkwnW5Cw1wQqu455WrA"),
        ("Feel Better, Live More", "UCDnwlb3IQDPJtFysPUJbDFQ"),
        ("ZOE Science & Nutrition", "UCa09am-cOsC-FSgr_nLkFFA"),
        ("Ten Percent Happier", "UCb3AWCFuxotrXmgqUHQdwyg"),
        ("The Doctor's Farmacy", "UC5IuDMmKWSsBFB0iKky6aEQ"),
        ("Pursuit of Wellness", "UCAUN0lbKJFUVzrys9qtgbdw"),
    ],
    "History": [
        ("The Rest Is History", "UCUYK0BJZF3yNb2fw1EdAXUQ"),
        ("Fall of Civilizations", "UCT6Y5JJPKe_JDMivpKgVXew"),
        ("Dan Carlin's Hardcore History", "UC3RcjbuyF5M1U4R62zjE3hg"),
        ("Empire", "UCN72FjV-mKjktLiaiV3zzJw"),
        ("The Ancients (History Hit)", "UCZwU2G-KVl-P-O-B35chZOQ"),
        ("Real Dictators", "UC0695hL5RjZEnmCWhXhSyxw"),
        ("Behind the Bastards", "UCJPHc5wprYeNF0K5ITwPBgA"),
        ("The Rest Is Classified", "UCiP_U09vcptxT5S72jUmZgA"),
    ],
    "Kids & Family": [
        ("Story Pirates", "UCJ3X66g1o0gOcwAvTmZa0PQ"),
        ("Good Inside with Dr. Becky", "UCQcifo_12x84Uji6h1TVmKg"),
        ("Calm Parenting Podcast", "UC5mQVgSA3OT3iba4BZAh-2w"),
        ("ParentData with Emily Oster", "UCbZpfvf0BfHBvEuH8d9Fk9Q"),
        ("Brains On!", "UCHWOq5gBWXZ4OcI8asZoVTQ"),
        ("Greeking Out (Nat Geo Kids)", "UCXVCgDuD_QCkI7gTKU7-tpg"),
        ("Raising Good Humans", "UCnvXghklCyl2wAmv450OLDg"),
    ],
    "Leisure": [
        ("Critical Role", "UCpXBGqwsBkpvcYjsJBQ7LEQ"),
        ("Kinda Funny Gamescast", "UCT6QFE3peNry9PdO5uGj96g"),
        ("Friends Per Second", "UCDRBt6MH-3IBsX5mWuq8ndQ"),
        ("The Smoking Tire", "UCgeGealT0QYcrnoYRMltDZg"),
        ("Trash Taste", "UCcmxOGYGF51T1XsqQLewGtQ"),
        ("Giant Bombcast", "UCmeds0MLhjfkjD_5acPnFlQ"),
        ("Tales from the Stinky Dragon", "UCRgIiwmdDoT_es43LbFxWKQ"),
        ("Dungeons and Daddies", "UCk0KIpWaQwMhFSWQhnwb1RQ"),
        ("The Besties", "UCp-USrGl6jkz7A8tomqT5rg"),
        ("C-Squared Podcast", "UCQWKH5DiO9_gBAQWQAaBnow"),
    ],
    "Music": [
        ("The Joe Budden Podcast", "UC23_r1bpkTWaBltbXsQxysA"),
        ("Drink Champs", "UCUseCJIxUbK_WIn0sUvBZVg"),
        ("Million Dollaz Worth of Game", "UCPXDl7FNZ3no_7OrvY_tlBg"),
        ("Zach Sang Show", "UCAJnnJPeWf45gPDuQ15-Z6w"),
        ("Tetragrammaton with Rick Rubin", "UC5Gat6FdyiG5ydUUHqPTAEQ"),
        ("Dissect", "UCaiXlXOldcHLgNWUiY3mf5A"),
        ("New Rory & MAL", "UCOnh0w4wdA-AkNTw3FUwAvA"),
        ("Song Exploder", "UCAm166_S0e3FAkO5GQmblbg"),
        ("Switched on Pop", "UC1jWWGWFwqSrpN1TninQQow"),
        ("Broken Record", "UCZ8_-dTIxnLOLt3jfH5XHJw"),
    ],
    "News": [
        ("Breaking Points", "UCDRIjKy6eZOvKtOELtTdeUA"),
        ("The Young Turks", "UC1yBKRuGpC1tSM73A0ZjYjQ"),
        ("MeidasTouch", "UC9r9HYFxEQOBXSopFS61ZWg"),
        ("The Megyn Kelly Show", "UCzJXNzqz6VMHSNInQt_7q6w"),
        ("The Ben Shapiro Show", "UCnQC_G5Xsjhp9fEJKuIcrSw"),
        ("The David Pakman Show", "UCsVi25eNeCERSMdEv3Js49Q"),
        ("Democracy Now!", "UCzuqE7-t13O4NIDYJfakrhw"),
        ("Piers Morgan Uncensored", "UCatt7TBjfBkiJWx8khav_Gg"),
        ("The Majority Report", "UCl6bYXTH3EmnpB1VGVxEOGQ"),
    ],
    "Religion & Spirituality": [
        ("BibleProject", "UCVfwlh9XpX2Y_tQfjeln9QA"),
        ("The Bible in a Year (Fr. Mike Schmitz)", "UCVdGX3N-WIJ5nUvklBTNhAw"),
        ("Joel Osteen Podcast", "UCvxWyn4rfcI2H9APhfUIB1Q"),
        ("Elevation with Steven Furtick", "UCDDSmXjv5vXqXo6OYuu83CA"),
        ("Transformation Church (Michael Todd)", "UCLaQE1OWx96NfC1wAmekpqw"),
        ("Girls Gone Bible", "UCGbzCe9feKNpWXUJL_G5LjA"),
        ("The Deen Show", "UCXHz5brnR9qwqvQvF3VJdgQ"),
        ("On Being with Krista Tippett", "UCDZcnmxyaiunvPdaBwL264A"),
        ("Mufti Menk", "UCNB_OaI4524fASt8h0IL8dw"),
        ("Pints with Aquinas", "UClh4JeqYB1QN6f1h_bzmEng"),
    ],
    "Science": [
        ("StarTalk", "UCqoAEDirJPjEUFcF2FklnBA"),
        ("Sean Carroll's Mindscape", "UCRhV1rWIpm_pU19bBm_2RXw"),
        ("Theories of Everything with Curt Jaimungal", "UCdWIQh9DGG6uhJk8eyIFl1w"),
        ("Into the Impossible with Brian Keating", "UCmXH_moPhfkqCk6S3b9RWuw"),
        ("Radiolab", "UCaum_fMDGgFQCmKHUBPq_xg"),
        ("Hidden Brain", "UCgjZeiV0Ks3Shx8xPgvm7pQ"),
        ("Ologies with Alie Ward", "UCRdDtLYr7X01yFlebvwiO7w"),
        ("The Skeptics' Guide to the Universe", "UCpeGBKn0axOJAcPHkcPiXcg"),
        ("The Poetry of Reality with Richard Dawkins", "UCH_zYYXkJpULueOVZTkY4Bw"),
    ],
    "Society & Culture": [
        ("The Joe Rogan Experience", "UCzQUP1qoWDoEbmsQxvdjxgQ"),
        ("Armchair Expert", "UClKP53RewJWK5s5WtLSg7Dg"),
        ("Call Her Daddy", "UCyGi3eCuxko37WB6uUr7LjA"),
        ("Shawn Ryan Show", "UCkoujZQZatbqy4KGcgjpVxQ"),
        ("Soft White Underbelly", "UCCvcd0FYi58LwyTQP9LITpA"),
        ("Freakonomics Radio", "UCXjf7anLJA4NqUv8kPFIJWA"),
        ("Stuff You Should Know", "UCTb6Oy0TXI03iEUdRMR9dnw"),
        ("H3 Podcast", "UCLtREJY21xRfCuEKvdki1Kw"),
        ("What Now? with Trevor Noah", "UC8bTQzxgvKkXDAaWkeuUlkg"),
    ],
    "Sports": [
        ("The Pat McAfee Show", "UCxcTeAKWJca6XyJ37_ZoKIQ"),
        ("New Heights with Jason and Travis Kelce", "UCVRm2Ho8cL3lvWDyp2ayuFw"),
        ("Pardon My Take", "UC9PKpPDyCi_Jck8j3e6N-_g"),
        ("Club Shay Shay", "UCQoxJOkwaCgyzQtiuAIDcuw"),
        ("Nightcap", "UCKnodHJpZd8UbSvAufDd3_g"),
        ("The Bill Simmons Podcast", "UCP032AGFh2KzzIUGylB9BjA"),
        ("The Overlap", "UCjXIw1GlwaY1IzpW_jN9iCQ"),
        ("Mind the Game", "UC6L_LBqoKZXFa4WxHox5iCw"),
        ("The Dan Le Batard Show", "UCAIZ314ffmfM4PynCR8vElw"),
        ("That Peter Crouch Podcast", "UCFULBvlxNWW8cWsrV6fGrcw"),
    ],
    "Technology": [
        ("Lex Fridman Podcast", "UCSHZKyawb77ixDdsGog4iWA"),
        ("Waveform: The MKBHD Podcast", "UCEcrRXW3oEYfUctetZTAWLw"),
        ("Hard Fork", "UCZcR2SVWaGWNlMqPxvQS3vw"),
        ("Darknet Diaries", "UCMIqrmh2lMdzhlCPK5ahsAg"),
        ("This Week in Tech (TWiT)", "UCwY9B5_8QDGP8niZhBtTh8w"),
        ("Dwarkesh Podcast", "UCXl4i9dYBrFOabk0xGmbkRA"),
        ("a16z Podcast", "UC9cn0TuPq4dnbTY-CBsm8XA"),
        ("BG2 Pod", "UC-yRDvpR99LUc5l7i7jLzew"),
        ("The Vergecast", "UCddiUEpeqJcYeBxX1IVBKvQ"),
        ("Machine Learning Street Talk", "UCMLtBahI5DMrt0NPvDSoIRQ"),
    ],
    "True Crime": [
        ("Rotten Mango", "UC0JJtK3m8pwy6rVgnBz47Rw"),
        ("MrBallen", "UCtPrkXdtCM5DACLufB9jbsA"),
        ("Murder, Mystery & Makeup (Bailey Sarian)", "UCtNdVINwfYFTQEEZgMiQ8FA"),
        ("Kendall Rae", "UCKBaL17hXLGJvi2KZKpja5w"),
        ("Crime Junkie", "UCpRiMGGX2hM9zqv6FgXXbKg"),
        ("Morbid", "UCnUc2Wte_U8eEljRlm30O0w"),
        ("Coffeehouse Crime", "UCcUf33cEPky2GiWBgOP-jQA"),
        ("That Chapter", "UCL44k-cLrlsdr7PYuMU4yIw"),
        ("Dateline NBC", "UCu3sKtOW9TGaErPsAYVPplA"),
        ("48 Hours", "UC7htuVs06oduI3xSfTdcxPA"),
    ],
    "TV & Film": [
        ("Happy Sad Confused", "UCf3SpupfoGnXEpgaGRGPbqg"),
        ("Office Ladies", "UCv8PYyVJgkyGkYohHm_8ANw"),
        ("The Always Sunny Podcast", "UCBw0bL8MtOIl1UX7db2MT3A"),
        ("Kermode and Mayo's Take", "UCCxKPNMqjnqbxVEt1tyDUsA"),
        ("The Rewatchables", "UCp7r5eXpWMerwO07ttoHmRg"),
        ("The Rest Is Entertainment", "UCYJpsKWYfZU8kOHZf_tUzQw"),
        ("Inside of You with Michael Rosenbaum", "UCvsIlofcT7DF3Kk8pz9yFfw"),
        ("Pod Meets World", "UCGkGz_U-0_WWjBu7E8onH_Q"),
        ("Fly on the Wall with Dana Carvey and David Spade", "UC8z2DCz-qmUrXti3dyz0FaQ"),
        ("Drama Queens", "UCcN3k6yTqrmbM697wmp7-Nw"),
    ],
}


def seed_category(category: str, db) -> tuple[int, int]:
    """Inserts the curated rows for `category` that aren't in the table yet,
    keyed (genre, channel_id) like everything else in genre_artists, and
    applies the committed profile to any existing row still missing one.
    Returns (added, updated); does not commit — the caller decides the
    transaction boundary (same contract as seed_genre).

    The update half matters on an install seeded before scripts/
    channel_profiles.py covered these channels: the rows are already there
    and correct, so an insert-only pass would skip them and leave the wizard
    showing them without avatars forever.
    """
    existing_by_channel = {
        row.channel_id: row
        for row in db.query(GenreArtist).filter(GenreArtist.genre == category)
    }
    added = updated = 0
    for show_name, channel_id in CURATED_CHANNELS[category]:
        profile = PROFILES.get(channel_id)
        row = existing_by_channel.get(channel_id)
        if row is None:
            existing_by_channel[channel_id] = build_row(category, show_name, channel_id, profile)
            db.add(existing_by_channel[channel_id])
            added += 1
        elif profile and row.thumbnail_url != profile[1]:
            row.title, row.thumbnail_url = profile
            row.resolved_at = utcnow()
            updated += 1
    return added, updated


def main() -> None:
    # Fail loudly on drift in either direction: a category renamed in
    # app/genres.py without a matching curated list, or a curated list whose
    # key no longer matches anything the onboarding wizard can send.
    assert set(CURATED_CHANNELS) == set(PODCAST_CATEGORIES), (
        set(CURATED_CHANNELS) ^ set(PODCAST_CATEGORIES)
    )
    with SessionLocal() as db:
        for category in PODCAST_CATEGORIES:
            added, updated = seed_category(category, db)
            db.commit()
            total = len(CURATED_CHANNELS[category])
            if added or updated:
                print(f"{category}: +{added} ~{updated} ({total} curated)")
            else:
                print(f"{category}: already seeded")


if __name__ == "__main__":
    main()
