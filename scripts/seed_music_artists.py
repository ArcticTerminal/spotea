"""Hand-curated replacement for the MusicBrainz tag-search seeding that
scripts/seed_genre_artists.py used to do for the onboarding wizard's
genre-artist cache (see app/services/genre_artists.py and app/models.py's
GenreArtist for the table shape).

Why hand-curated: MusicBrainz *tag search* surfaces arbitrary artists that
happen to carry a genre tag, with no notion of fame or representativeness —
a new user tapping the "Rock" chip expects Queen and AC/DC, not whichever
tagged act MusicBrainz's search relevance happens to rank first. So the
artists below were picked by hand (10-12 genuinely famous, representative
acts per genre). MusicBrainz still earned its keep at research time, just
in a different role: its editor-curated url-relationships served as the
name -> official-YouTube-channel resolver for most entries (the rest came
from the artists' own youtube.com/@handle pages), and every resolved
channel was verified by title — auto-generated "<Artist> - Topic" channels
were rejected outright, since Topic-sourced tracks are frequently
region-locked and make terrible onboarding suggestions.

Run from the repo root:
    .venv/bin/python -m scripts.seed_music_artists

Wipe-and-replace per genre, but only for genres in MUSIC_GENRES — rows for
other genres (the podcast categories' curated channels live in the same
table) are never touched. Safe to re-run: a genre whose rows already match
the curated list exactly is skipped without deleting anything, which also
preserves the lazily-resolved YouTube display metadata on those rows
(title/thumbnail/subscribers — see get_suggested_channels_by_genre).
"""

from app.database import SessionLocal
from app.genres import MUSIC_GENRES
from app.models import GenreArtist
from app.services.genre_artists import build_row
from scripts.channel_profiles import PROFILES

# (artist_name, channel_id) per genre; channel_url is derived from the id.
# Keys must match app/genres.py's MUSIC_GENRES exactly — main() fails loudly
# on any mismatch so a renamed or added genre can't silently seed nothing.
CURATED_ARTISTS: dict[str, list[tuple[str, str]]] = {
    "Pop": [
        ("Taylor Swift", "UCANLZYMidaCbLQFWXBC95Jg"),
        ("Ariana Grande", "UC0VOyT2OCBKdQhF3BAbZ-1g"),
        ("Ed Sheeran", "UC0C-w0YjGpqDXGB8IHb662A"),
        ("Billie Eilish", "UCiGm_E4ZwYSHV3bcW1pnSeQ"),
        ("Dua Lipa", "UC-J-KZfRV8c13fOCkhXdLiQ"),
        ("Bruno Mars", "UCoUM-UJ7rirJYP8CQ0EIaHA"),
        ("The Weeknd", "UC0WP5P-ufpRfjbNrmOWwLBQ"),
        ("Justin Bieber", "UCHkj014U2CQ2Nv0UZeYpE_A"),
        ("Adele", "UComP_epzeKzvBX156r6pm1Q"),
        ("Katy Perry", "UCYvmuw-JtVrTZQ-7Y4kd63Q"),
        ("Lady Gaga", "UC07Kxew-cMIaykMOkzqHtBQ"),
        ("Rihanna", "UC2xskkQVFEpLcGFnNSLQY0A"),
    ],
    "Rock": [
        ("Queen", "UCiMhD4jzUqG-IgPzUmmytRQ"),
        ("AC/DC", "UCB0JSO6d5ysH2Mmqz5I9rIw"),
        ("The Beatles", "UCc4K7bAqpdBP8jh1j9XZAww"),
        ("The Rolling Stones", "UCB_Z6rBg3WW3NL4-QimhC2A"),
        ("Led Zeppelin", "UCaKZA66vM_TUpetUNohmR0A"),
        ("Foo Fighters", "UCGRjJrpD2bmk9Ilq6nq80qg"),
        ("Red Hot Chili Peppers", "UCEuOwB9vSL1oPKGNdONB4ig"),
        ("Linkin Park", "UCZU9T1ceaOgwfLRq7OKFU4Q"),
        ("U2", "UC4gPNusMDwx2Xm-YI35AkCA"),
        ("Coldplay", "UCDPM_n1atn2ijUwHd0NNRQw"),
        ("Green Day", "UCqC_GY2ZiENFz2pwL0cSfAw"),
        ("Aerosmith", "UCBxdHQVOaZhUOIj_3gt2FYw"),
    ],
    "Hip-Hop": [
        ("Eminem", "UC20vb-R_px4CguHzzBPhoyQ"),
        ("Drake", "UCByOQJjav0CUDwxCk-jVNRQ"),
        ("Kendrick Lamar", "UC3lBXcrKFnFAFkfVk5WuKcQ"),
        ("Kanye West", "UCs6eXM7s8Vl5WcECcRHc2qQ"),
        ("JAY-Z", "UCN-sc1xJr-QQNj_uNIM9wTA"),
        ("Snoop Dogg", "UCL7CNO0U9SjWFbu7veoLRBw"),
        ("Nicki Minaj", "UCaum3Yzdl3TbBt8YUeUGZLQ"),
        ("Travis Scott", "UClRx3MMyYUyqOxyEqA5F2nQ"),
        ("J. Cole", "UCnc6db-y3IU7CkT_yeVXdVg"),
        ("50 Cent", "UC0sL86jDLgs2fGRD-XBnPQA"),
        ("Lil Wayne", "UCEOhcOACopL42xyOBIv1ekg"),
        ("Cardi B", "UCxMAbVFmxKUVGAll0WVGpFw"),
    ],
    "R&B": [
        ("Beyoncé", "UC9zX2xZIJ4cnwRsgBpHGvMg"),
        ("Usher", "UCaNrhBiXsXIM2epDl_kEzgQ"),
        ("Alicia Keys", "UCETZ7r1_8C1DNFDO-7UXwqw"),
        ("Chris Brown", "UCcYrdFJF7hmPXRNaWdrko4w"),
        ("SZA", "UCO5IQ70V7l-XpHW40HwaGsw"),
        ("Frank Ocean", "UCf9wumweDPswxIrex0TaZuw"),
        ("Mariah Carey", "UClS0wn3LPs9jdX_yt2g1k8w"),
        ("John Legend", "UCEa-JnNdYCIFn3HMhjGEWpQ"),
        ("H.E.R.", "UCFwC3Ryue6CorFm2xCEG0Aw"),
        ("Ne-Yo", "UCa5R55NSXiWAZWi9997XYvg"),
        ("Summer Walker", "UC4NBMVsQp6QvIjzUeaqX3ZA"),
        ("Brent Faiyaz", "UCtdVC7nSYNOpKroA5JicNig"),
    ],
    "Electronic": [
        ("Daft Punk", "UC_kRDKYrUlrbtrSiyu5Tflg"),
        ("Calvin Harris", "UCaHNFIob5Ixv74f5on3lvIw"),
        ("David Guetta", "UC1l7wYrva1qCH-wgqcHaaRg"),
        ("Avicii", "UC1SqP7_RfOC9Jf9L_GRHANg"),
        ("Marshmello", "UCEdvpU2pFRCVqU6yIPyTpMQ"),
        ("Alan Walker", "UCJrOtniJ0-NWz37R30urifQ"),
        ("Martin Garrix", "UC5H_KXkPbEsGs0tFt8R35mA"),
        ("deadmau5", "UCYEK6xds6eo-3tr4xRdflmQ"),
        ("Kygo", "UCCFJeI-2sT_cWgz-QJRgbCw"),
        ("Zedd", "UCFzm6oAGFmmZfkrzQ5wATSQ"),
        ("The Chemical Brothers", "UCFhSm5HFBvZkldbOxeVoidQ"),
        ("Flume", "UCXAhoI7XO2kafTMjocm0jCg"),
    ],
    "Jazz": [
        ("Miles Davis", "UC1ZS17c0DlqUjsXZK3K_bgA"),
        ("John Coltrane", "UCGiKlUaxFFNXkEYIW6mfbBQ"),
        ("Ella Fitzgerald", "UC63nGmKVdVhiZr_bWhJVGGg"),
        ("Frank Sinatra", "UCJtvg6ZFwzdFdtcHBqetvwg"),
        ("Nina Simone", "UCJ-FRbWianyv9q-Ly9whFQQ"),
        ("Billie Holiday", "UCwO6O7HAOHQ7_FgI28QWtvQ"),
        ("Herbie Hancock", "UC-w3aQyKGcYe9JnhnhExaNQ"),
        ("Norah Jones", "UCM11Z1jQPm07ImX4lqYmwqA"),
        ("Diana Krall", "UCqh7IaOk6Skx-gEYMGN3UwA"),
        ("Dave Brubeck", "UCt9j7GVh_NwJjbnFcJItReA"),
    ],
    "Classical": [
        ("Ludovico Einaudi", "UC8pY8FYkA4BXhhtqfw71MfA"),
        ("Yo-Yo Ma", "UCHWa5v6bbSxpns7mUfC3e5g"),
        ("Lang Lang", "UCkEZRZfqwPfv8JqQ3u1n2Ug"),
        ("Hans Zimmer", "UCJeBQabyLa_FvMxb6G67lkw"),
        ("Andrea Bocelli", "UCb4JB8-ZAeceuR7EPCPOPzg"),
        ("Max Richter", "UC-n5eIZ0AecwjWyXYeSwXkA"),
        ("Hilary Hahn", "UCUKqvWY5e2VZA-L6RT-ACpw"),
        ("Berliner Philharmoniker", "UCtRkmSO4PrhJ4TzNOmFIwjw"),
        ("London Symphony Orchestra", "UCY1yTIi-DaxPbNtLCnwAM1g"),
        ("The Piano Guys", "UCmKurapML4BF9Bjtj4RbvXw"),
        ("2CELLOS", "UCYpdHLYhFchZ1tKO9TaOqWw"),
    ],
    "Metal": [
        ("Metallica", "UCbulh9WdLtEXiooRcYK7SWw"),
        ("Iron Maiden", "UCaisXKBdNOYqGr2qOXCLchQ"),
        ("Black Sabbath", "UCrx-X329UKv0Y06VhfpFVvw"),
        ("Slipknot", "UCOJZ1tna8yj8mAEITPkHNCQ"),
        ("Rammstein", "UCYp3rk70ACGXQ4gFAiMr1SQ"),
        ("Megadeth", "UC-Ugu5IBFgrYuHBAW2DjmZQ"),
        ("Pantera", "UChTDORxN3YPmasEurM6kRoA"),
        ("Judas Priest", "UC48vpdaG8NDvEGLj11XPPZQ"),
        ("Slayer", "UC1Ql0sYH76_Zd1ru4phfX3g"),
        ("System of a Down", "UC7-YMmnc0ppcWmio8t1WdcA"),
        ("Avenged Sevenfold", "UCFcqi7MrlzIp9RMTtUlxE8g"),
        ("Ghost", "UCAOiVaJJlH0Oduv48NN0mMA"),
    ],
    "Indie": [
        ("Arctic Monkeys", "UC-KTRBl9_6AX10-Y7IKwKdw"),
        ("The Strokes", "UC28OQSWdiW7jkadedZRRyZA"),
        ("Tame Impala", "UCdI8MAC5HoPJSJ4zrgDDI-Q"),
        ("Vampire Weekend", "UCUGzhBmck61sa3eGxp9xI8A"),
        ("Florence + the Machine", "UC5MujsH-hrVWHBBfSSmv18A"),
        ("The 1975", "UC_LfW1R3B0of9qOw1uI-QNQ"),
        ("Cage the Elephant", "UCNMceJPDpB-1ZQJzh2PZ1qw"),
        ("MGMT", "UCC2i4uIWWjx5L5V5Q5k7eiQ"),
        ("Mac DeMarco", "UCqnMk5GA1spXDiHYFcPN-eA"),
        ("Phoenix", "UCSI2VLXew910P_SPAPtyf8Q"),
        ("Two Door Cinema Club", "UC21j22BUI19MJ6F8IrxnjJg"),
        ("alt-J", "UCAMWWQQNQeD73DtiddfBrpg"),
    ],
    "Lo-fi": [
        ("Lofi Girl", "UCSJ4gkVC6NrvII8umztf0Ow"),
        ("Chillhop Music", "UCOxqgCwgOqC2lMqC5PYz_Dg"),
        ("College Music", "UCWzZ5TIGoZ6o-KtbGCyhnhg"),
        ("Nujabes", "UC1Rx706koNXvCptlbS_nYQg"),
        ("the bootleg boy", "UC0fiLCwTmAukotCXYnqfj0A"),
        ("Dreamhop Music", "UCz9_4daWw-uWuqeB6_IkhMg"),
        ("jinsang", "UCHVRJMrLrRqrf9zqEDfFjpg"),
        ("potsu", "UCqBGzCfgTSOU24xbenjhLaw"),
        ("Kupla", "UCKfqkjTS2gp-vsOiXkzKjcA"),
        ("eevee", "UCPUAkFysGwOgB5ibTPSN4lg"),
    ],
    "K-Pop": [
        ("BTS", "UCiPNSjTu0sFVUUFfhPCdEsA"),
        ("BLACKPINK", "UCOmHUn--16B90oW2L6FRR3A"),
        ("TWICE", "UCCRb6nYKaT8tzLA8CwDdUtw"),
        ("Stray Kids", "UC9rMiEjNaCSsebs31MRDCRA"),
        ("SEVENTEEN", "UCfkXDY7vwkcJ8ddFGz8KusA"),
        ("EXO", "UCzCedBCSSltI1TFd3bKyN6g"),
        ("Red Velvet", "UCk9GmdlDTBfgGRb7vXeRMoQ"),
        ("NewJeans", "UCMki_UkHb4qSc0qyEcOHHJw"),
        ("TXT", "UCtiObj3CsEAdNU6ZPWDsddQ"),
        ("aespa", "UC9GtSLeksfK4yuJ_g1lgQbg"),
        ("IVE", "UC-Fnix71vRP64WXeo0ikd0Q"),
        ("(G)I-DLE", "UCritGVo7pLJLUS8wEu32vow"),
    ],
    "Latin": [
        ("Bad Bunny", "UCmBA_wu8xGg1OfOkfW13Q0Q"),
        ("Shakira", "UCGnjeahCJW1AF34HBmQTJ-Q"),
        ("J Balvin", "UCrHL_BF5lHyK43BxLU8-vBQ"),
        ("Karol G", "UCz9yS18zJGQObwUL_K-ICnw"),
        ("Daddy Yankee", "UC5cqeAzY9MJBiSuAtOlv6LQ"),
        ("Maluma", "UCFkoPRmuxqr37jvGmmpzhzQ"),
        ("Ozuna", "UC2ae_kdIhrGaPOqwh1bwGuQ"),
        ("Luis Fonsi", "UCxoq-PAQeAdk_zyg8YS0JqA"),
        ("Enrique Iglesias", "UC-6czyMkxDi8E8akPl0c7_w"),
        ("Peso Pluma", "UCzrM_068Odho89mTRrrxqbA"),
        ("Rauw Alejandro", "UC_Av98lDjf5KvFib5elhpYg"),
        ("Becky G", "UCgtNC51EUSgcZ6kKyVoPxKA"),
    ],
    "Country": [
        ("Johnny Cash", "UCLwdOhL6TKbmjRtZ8wIr-Bg"),
        ("Dolly Parton", "UCuGuRQHrvoNsD-AolV0X39g"),
        ("Luke Combs", "UCwLK17rpMFUfktQEcMzSyWg"),
        ("Morgan Wallen", "UCzIyoPv6j1MAZpDHKLGP_eA"),
        ("Carrie Underwood", "UCBxZZfQ8R2xtk0YEU1d8l4Q"),
        ("Chris Stapleton", "UCsdXkstc8jFC3zpMYdEz_zA"),
        ("Shania Twain", "UCadSacAVwG-QH6tWzjrmjxA"),
        ("Blake Shelton", "UC9nxc-xiH1AnL9RGstfgurg"),
        ("Kacey Musgraves", "UCAxUOuaNs8e63_MRqhTdmaQ"),
        ("Keith Urban", "UCHYqkMN0UwbyN0krs1pahXQ"),
        ("Zach Bryan", "UCwK3C8Vgphad4PweezfUBAQ"),
        ("Willie Nelson", "UCdBD6uy0ZuX7sLK4__Ll6LQ"),
    ],
    "Reggae": [
        ("Bob Marley & The Wailers", "UCj7aFcg5yNZL9dhpuuYLpbQ"),
        ("Damian Marley", "UC-hlSyeIiuM7zS1YS0jhz2w"),
        ("Ziggy Marley", "UCyWgTmPWR1WTpXdUYYIDYQA"),
        ("Sean Paul", "UCkdc7gHpavxpgGalxKbgSHA"),
        ("Shaggy", "UCBtln7sL3FYAY6ePguz9sNg"),
        ("UB40", "UC6jd0WZLAp0LGcAyiJiDgBQ"),
        ("Chronixx", "UCJnYM0A1ec_yqU3vpNGWyAw"),
        ("Protoje", "UCYZo7CbLJjjfEOqYwknzzow"),
        ("Toots and the Maytals", "UClZN1V3nIkjF-6bRVQkiNPg"),
        ("Burning Spear", "UC-IgH9OyLNNio7nWEx9L2Kw"),
        ("Alborosie", "UCr9Udhx6RQR_-zWxVio3ENA"),
        ("Koffee", "UCUUNMri9GAoH2PiXCQJ6aWw"),
    ],
    "Folk": [
        ("Bob Dylan", "UCnRI0ay61tY-fKYzzB3fCnw"),
        ("Simon & Garfunkel", "UCNErQXrdIIZEEdkP54tPsRg"),
        ("Joni Mitchell", "UC1wN41s_8Fmew81Pk2LQOxw"),
        ("Mumford & Sons", "UChPzV-uAKhVMguKI7RB8uGQ"),
        ("The Lumineers", "UCB7P9Hr5BYB5Mkxau6t3Sgw"),
        ("Fleet Foxes", "UCna-InS-5L5qv82diztOT7A"),
        ("Bon Iver", "UCci2c90HJbY0VAS3_eLF3Wg"),
        ("Iron & Wine", "UCzEOgPvZv8BZjVkfmwjtpAQ"),
        ("José González", "UCOxQXnxLdxQjvu9FHk1jsdQ"),
        ("Sufjan Stevens", "UCMi8tQF_L7rd6YcmSt7MXrQ"),
        ("Nick Drake", "UCw00GrG5ax6BsymeAgU3wmw"),
        ("First Aid Kit", "UCMV41gD04aZot3TmN0PFHsQ"),
    ],
    "Punk": [
        ("Ramones", "UCSrA5JaXpR21z_E1FDA03RA"),
        ("Sex Pistols", "UCcjWoLUPOkEVhevjI8DB5vQ"),
        ("The Clash", "UCd3r4AlUO5QQrvF3iDHsjPw"),
        ("Green Day", "UCqC_GY2ZiENFz2pwL0cSfAw"),
        ("blink-182", "UCdvlHk5SZWwr9HjUcwtu8ng"),
        ("The Offspring", "UCF3EnnuLjeab3r8l1k2rV9g"),
        ("Bad Religion", "UCkm5ovgOWYV9L15Rf0H2Wug"),
        ("NOFX", "UC_dHsUBpGtY0LcW0DiOsrXA"),
        ("Rancid", "UCFSjnN55tV-mecyG0mYvhdQ"),
        ("Misfits", "UCKkLIDce12KiuxCfOwVGPbA"),
        ("Dead Kennedys", "UCHnovAeuCPoqrZy7YzxG9GA"),
        ("Rise Against", "UChMKB2AHNpeuWhalpRYhUaw"),
    ],
    "Soul": [
        ("Aretha Franklin", "UCSuWu2ZVL3kRmKtCggqskQQ"),
        ("Stevie Wonder", "UCGD7CfG3JgZF52QpIRivV1Q"),
        ("Marvin Gaye", "UCq1KEhv1y-z6QEFIaMccLxg"),
        ("Al Green", "UCiE5dPVpnFpPl8ki4QjLH5Q"),
        ("Sam Cooke", "UCaJ_FGoswaywfpCd8xJc4hw"),
        ("Amy Winehouse", "UCHai12P6Gh7PaIYZGnzyrSA"),
        ("Leon Bridges", "UCD8FVPChed3F3CH-xPHwf4A"),
        ("D'Angelo", "UCKCWQOVUfdlDYX2_238_AeQ"),
        ("Gladys Knight", "UC5TQdoadyzC48CUDc95m0iw"),
        ("Bill Withers", "UCBTrTXbSiv4Pq7As_tcB0UA"),
    ],
    "World": [
        ("Cesária Evora", "UCs5WMDtJoMaX5vIMus_cPhg"),
        ("Angélique Kidjo", "UCminozacV4jDfaQohcSFyCQ"),
        ("Ali Farka Touré", "UCm0WuygL0rAKAGyPnEDDb-w"),
        ("Tinariwen", "UCZ3wH-v5zOcq4ZZWI58TGKA"),
        ("Ladysmith Black Mambazo", "UC9sRuwsWqUPcHodXntcrsRA"),
        ("Gipsy Kings", "UCMSvtHZgu0-n6ISejwL8rAw"),
        ("Rodrigo y Gabriela", "UC2aEvw7VlapqdI6TB6AMjNQ"),
        ("Anoushka Shankar", "UCVWj1AEkMoN4sUBewx9WDsw"),
        ("Salif Keita", "UCEs_QdHpYe6Rf4-KYAjszyQ"),
        ("Baaba Maal", "UCCpHXDm-tPU7BJRFCDRz5xQ"),
    ],
    "House": [
        ("Swedish House Mafia", "UC5HEq5U--O5nn134mizyCcw"),
        ("Disclosure", "UCTyZ4LCVRiCEVfkVqdi0m3A"),
        ("Duke Dumont", "UCQCtrgPAP6pjxLeVFTKJfhA"),
        ("Black Coffee", "UC0cUnMCsopLMon3_lXYTx_g"),
        ("Oliver Heldens", "UC-EVnno6x6-aAG6g1ZVoN3A"),
        ("Purple Disco Machine", "UCdkMBTZmOXDh8nTSX1RdRkA"),
        ("MK", "UCX7aAyl_kckfFGCTHXnD5qA"),
        ("Jax Jones", "UCj95pmTj8-hClQPPc972VOw"),
        ("Dom Dolla", "UCUPWpXNpsqc0A_IWvBZRSOw"),
        ("Robin Schulz", "UCLVVBWrp9jw4-SYUoU42hcg"),
        ("Lost Frequencies", "UCdKS_mDSLUkS6vDK6u1mjOg"),
    ],
    "Techno": [
        ("Charlotte de Witte", "UC-yOW3e6zBSo1JwLXq46Suw"),
        ("Amelie Lens", "UCg2JFUP67ZdKzehy8TWMUmw"),
        ("Carl Cox", "UCCrHGOX6Uoj5PndNIoRkoFw"),
        ("Boris Brejcha", "UCukezONa4veoJBeK9UuVZew"),
        ("Adam Beyer", "UClJz6TjG4c3_XymRzJTm4aQ"),
        ("Richie Hawtin", "UC2tkpbU5ELaFXI2Bn2WnaJg"),
        ("Nina Kraviz", "UCaL9gRXRUH_aJ599gMBRJcw"),
        ("Reinier Zonneveld", "UCkLsBswa8D01UmkmwAjjPHA"),
        ("Paul Kalkbrenner", "UCNmmySngBxHEG3YlsFLHorg"),
        ("Deborah de Luca", "UCzwZvELsW3A4D0wtJZvtznQ"),
    ],
    "Trance": [
        ("Armin van Buuren", "UCu5jfQcpRLm9xhmlSd5S8xw"),
        ("Above & Beyond", "UCVE-ybBDg3UHSUylEVdPAsw"),
        ("Paul van Dyk", "UCodQo76JWa75exuuEpucVeA"),
        ("Tiësto", "UCO59y9XJIJZS5i86lUOvMGA"),
        ("Ferry Corsten", "UCjNKLcXS79kibFItBzaOf8A"),
        ("Aly & Fila", "UCNVeD_tHABqF-fvbe20ZsPA"),
        ("Markus Schulz", "UCxTpYBc13Uq1WMOoweBvPyg"),
        ("ATB", "UCK-INiAxSg27L8DQ6HIoHGQ"),
        ("Paul Oakenfold", "UC3J9EIm0n-NU1RBPlsWIT7g"),
        ("Cosmic Gate", "UCtv5j1LpJeej5f5fzYfLZZw"),
        ("Gareth Emery", "UCsvSix9-QrU0kNn5Ot2LYkw"),
        ("Dash Berlin", "UCApnql05Ym89GCXAyv0WZxA"),
    ],
    "Dubstep": [
        ("Skrillex", "UCMYbLgav7tU6jjl8gMi1C2Q"),
        ("Excision", "UCVvv7hEv2StiEdNarVoWelQ"),
        ("Zomboy", "UC5_s37EekKcAqm2MiL5uqCQ"),
        ("Flux Pavilion", "UCweZOSx5IoP4FN9Vv1cK8tw"),
        ("Virtual Riot", "UCVtJOq_ziepf5MpjsTWxJeg"),
        ("Subtronics", "UCTzj8xeshbW4yl2ACfQFjsg"),
        ("Zeds Dead", "UCsYkUlicwVBtW-pAInUSyPA"),
        ("Knife Party", "UCtXRolzmkBDmAkTEJiFXohw"),
        ("Nero", "UCtbUrMDlQY0QQRSqHL5JbRQ"),
        ("Rusko", "UCWO70BzyzBGGw-BqWpTl2ew"),
        ("SVDDEN DEATH", "UC7KzjYfJvfCTFvbMbw_BhUw"),
    ],
    "Drum & Bass": [
        ("Pendulum", "UCP2uxfZ9lfeqY2nkUkoPtvg"),
        ("Chase & Status", "UCSnULYPo-BA8zo5IcG4doIQ"),
        ("Netsky", "UCYju6cJkaQafQvKE9q-vMWQ"),
        ("Sub Focus", "UCWkMMrVPVVNxuJFHv4ApiDg"),
        ("Andy C", "UCa4HoeRdmCZ1JYy_jqLxFRw"),
        ("Wilkinson", "UCtaTjkogAC3Xh5J-ZPdX0BQ"),
        ("High Contrast", "UCmAmx8pXVxYFj8OYTFLmyCA"),
        ("Camo & Krooked", "UCxaLJvYDW8XMgrNbdnZ-uMQ"),
        ("Noisia", "UCPSso4A-41Rth8KMf0O40iw"),
        ("Goldie", "UC3jTmoAqoxGn_oIISd3f_eg"),
        ("Hybrid Minds", "UCp2ulyjpxHOVpQQWX67ylOw"),
    ],
    "Ambient": [
        ("Brian Eno", "UCDDJRnc_LsRm-_CAfRcKCZA"),
        ("Aphex Twin", "UC4hfA78X-lqiRERBZLTnLBw"),
        ("Tycho", "UCWzGCSf-1fluXYvVOuh-aVA"),
        ("Boards of Canada", "UC9D7VN2HldiRFdxFvCcyA7A"),
        ("Nils Frahm", "UCumpwYpXynIdMe8VYnGrw-Q"),
        ("Ólafur Arnalds", "UC9XfDqYSm6ezuw0y_u3t_4A"),
        ("Jon Hopkins", "UCiwgazsh4EbM04FIQuyDVYw"),
        ("Hammock", "UCJ5q1LpG-iISADNJ_E87C9g"),
        ("Solar Fields", "UCG5ga_-9l5e-FBRcMYfQjew"),
        ("Marconi Union", "UChBai3vOosxgbBHYKMs2Rog"),
        ("Moby", "UCkkiTV-Lnt-m3DQp-ZeFdkA"),
    ],
    "Funk": [
        ("James Brown", "UCOCZxe0gNRA7c3PGWPGoiGg"),
        ("Earth, Wind & Fire", "UCztiH7D-fHwsMyUF8r15vsQ"),
        ("Prince", "UCv3mNSNjuWldihk1DUdnGtw"),
        ("Kool & The Gang", "UCX8pxyrMe7tJDhpM2DRgHvw"),
        ("Chic", "UCl8SEpoDrxkD_Ld1teSat6A"),
        ("Sly and the Family Stone", "UCrPnFvHBoZz1326HwrKBOFg"),
        ("Vulfpeck", "UCtWuB1D_E3mcyYThA9iKggQ"),
        ("Jamiroquai", "UCd8wWXk6fSa8b8WaZbpWgvg"),
        ("Bootsy Collins", "UC_X5EHf-1NhP7KCiOBqlIVg"),
        ("Tower of Power", "UCkUg8IBMLEndTGCWwpM4t7Q"),
        ("George Clinton", "UCEcVmk43YMmc_6CrwrufEnA"),
        ("Cory Wong", "UCQqC08JWnJGJIgw43XJ0GXw"),
    ],
    "Blues": [
        ("B.B. King", "UCRBh8rjd8umQiMMNB9_5D_w"),
        ("Stevie Ray Vaughan", "UCX25kfBbEN1MNEPtlNLOycw"),
        ("Eric Clapton", "UCtCOFqqGGGunX71nfZgPQOQ"),
        ("Buddy Guy", "UC2xTrTBG-YzWxuALSI78_wA"),
        ("John Lee Hooker", "UCdLnBWRkJUD4JTL6cCZmQSg"),
        ("Gary Clark Jr.", "UCqOvoj8ToHKN5YzyBGZCgUQ"),
        ("Joe Bonamassa", "UCcCa2gD7AEA-6SVN8nZw_IA"),
        ("Keb' Mo'", "UCl9AgUauofjFUHMlOKd_K6Q"),
        ("Bonnie Raitt", "UCsKULr2K0JSPUfMZOVzQ4DQ"),
        ("Etta James", "UC_4PEiE57nftOns6EHjWtog"),
        ("Robert Cray", "UCp2CfRwTsbpsdCZTdUkOs5A"),
    ],
    "Gospel": [
        ("Kirk Franklin", "UCTbSDtpElugjahw088Nnubg"),
        ("Tasha Cobbs Leonard", "UC8aCcbdR6hWfeynEmrguPyg"),
        ("CeCe Winans", "UC86Zlc-v_kkZ-yKG2cJZkxQ"),
        ("Marvin Sapp", "UC7M9afaCP6r8QtCpHHaxVxQ"),
        ("Fred Hammond", "UCvP-zy6ObS1SyxMKEIWmwUA"),
        ("Tamela Mann", "UCc99m_j116DFKCkFYy7n6yg"),
        ("Donnie McClurkin", "UCQGjdGMZW1siONxmnasWlJg"),
        ("Sinach", "UCcaV40yrjS5R88xYHxK_0rA"),
        ("Travis Greene", "UC4jufEFPiBuZhH2QDul-mHw"),
        ("Maverick City Music", "UCeFWKvh1eNWWOq5pq2Z4rYg"),
    ],
    "Disco": [
        ("Bee Gees", "UCD9sCcKXnFxMeuFoNayVxeQ"),
        ("ABBA", "UCa_4DcdTB9QfK0LY9-7qWuQ"),
        ("Donna Summer", "UC6dX999wO3gBsS1qzm4I-KA"),
        ("Chic", "UCl8SEpoDrxkD_Ld1teSat6A"),
        ("Gloria Gaynor", "UCgVq3HlmkLoh9CFt9i7Syug"),
        ("Boney M.", "UCHFPpw9jReIhssGTUNUZBoA"),
        ("Earth, Wind & Fire", "UCztiH7D-fHwsMyUF8r15vsQ"),
        ("KC and the Sunshine Band", "UCHM4DtlvPVh9VdpmCo___Lw"),
        ("Village People", "UCeWkruiHm7NL7OvjPyVshkw"),
        ("Diana Ross", "UCAzvIPluG5x-iosA1C4LV9g"),
        ("Kool & The Gang", "UCX8pxyrMe7tJDhpM2DRgHvw"),
    ],
    "Grunge": [
        ("Nirvana", "UCzGrGrvf9g8CVVzh_LvGf-g"),
        ("Pearl Jam", "UClQT6Vnsm6BUm0I5kR26EkQ"),
        ("Soundgarden", "UCHKSayVT2Ks-gQBXmMLGTag"),
        ("Alice in Chains", "UChf0Knt-e9Pw8VywfuTZCjA"),
        ("Stone Temple Pilots", "UCocfdCiKujljqcGC-STMsCQ"),
        ("Mudhoney", "UCDTbr5CIsgVAwXxUIfukV6w"),
        ("Melvins", "UCaoEPkhVvqVQVv-BoaQl5bQ"),
        ("Bush", "UCEByXUIWdQJHPbo4t0Xs7qQ"),
        ("Silverchair", "UC7GKCAc8HNjraXz1bRy7Oaw"),
        ("Hole", "UCGE7WjDQBpG6bFA153iRVVw"),
        ("Screaming Trees", "UCfkD7WftBqx0WyiEC4emrfQ"),
        ("L7", "UCvNz35FxAbo7aRFWRRZsFVA"),
    ],
    "Hard Rock": [
        ("AC/DC", "UCB0JSO6d5ysH2Mmqz5I9rIw"),
        ("Guns N' Roses", "UCIak6JLVOjqhStxrL1Lcytw"),
        ("Van Halen", "UCP7SHuVeFOHi27A1VSseFPQ"),
        ("Aerosmith", "UCBxdHQVOaZhUOIj_3gt2FYw"),
        ("Def Leppard", "UCZjBqZjGsmI5OAVsLSroaWQ"),
        ("Bon Jovi", "UCkBwnm7GOfYHsacwUjriC-w"),
        ("Deep Purple", "UC3SF_Sa_dVWQ09m88nvsXrg"),
        ("Scorpions", "UCcvnDgwSH5Dl2b3Bxfz4OCQ"),
        ("Whitesnake", "UCaoAT9KQ564EWz9IRLN8HRw"),
        ("Mötley Crüe", "UC4OIQdGVhOAQx3IgrAnSzaw"),
        ("ZZ Top", "UCXdqh7TtuMuqasECfTItzXA"),
        ("Alice Cooper", "UCJQa4Ah9-Qf36bl1JhkAaYA"),
    ],
    "Emo": [
        ("My Chemical Romance", "UCCZGYab5SpD0I7Z5JqJZgww"),
        ("Fall Out Boy", "UC2qWxZHgnlwDvcmLqP23jrA"),
        ("Panic! at the Disco", "UColJTBTSGqaaZr5NOk5r3Pg"),
        ("Paramore", "UCc7_woMAIVIW2mAr1rPCsFQ"),
        ("Dashboard Confessional", "UC1hVJ5z8WQFROU9KCPWkapw"),
        ("Taking Back Sunday", "UC62VlucV7MFnEFPBatIOeLw"),
        ("Jimmy Eat World", "UCFeCvEjX56ReS-O3QHdyjbA"),
        ("The Used", "UCFwZUc0XGKdVBn7deieUasA"),
        ("Brand New", "UC1XxJbkGfgPPIfGJ0PU0SPw"),
        ("Hawthorne Heights", "UCCQ3ylb7cvGwgl8bGBJ7rfg"),
        ("Pierce the Veil", "UCyipvkWkNpmEQPGovalDjFA"),
        ("American Football", "UCW_XEF0vptbFHD4qrIvJTuw"),
    ],
    "Ska": [
        ("The Specials", "UCeNxuVVmAOl_L6E8hCymT8w"),
        ("Madness", "UC0iYPVu2agNaaqbnHFPCBFQ"),
        ("Sublime", "UCEY4Q-pEMeqhSKz30RZNfFw"),
        ("Reel Big Fish", "UCjO8iiof-PPyNHRSRJeKclw"),
        ("Less Than Jake", "UCierbPdWUu5-WN2p7r1j1-w"),
        ("Streetlight Manifesto", "UCI3eBKy-xEQPvroZmKxzX8A"),
        ("Toots and the Maytals", "UClZN1V3nIkjF-6bRVQkiNPg"),
        ("No Doubt", "UCtJXMyhnbdIQrCSDwAeJlNw"),
        ("Fishbone", "UCDJIMcwP7bH8Ya4vRjd3bcA"),
        ("The Interrupters", "UCHLYhebp9oaoqj_u0yG0ZYA"),
        ("Goldfinger", "UC4n5KgQloCiY_mYCQ6gRtfA"),
    ],
    "Afrobeat": [
        ("Fela Kuti", "UCiSx_RyMYooNKnxTl80983w"),
        ("Burna Boy", "UCEzDdNqNkT-7rSfSGSr1hWg"),
        ("Wizkid", "UCQvyMmwWTnVLuusjYg-zYVA"),
        ("Davido", "UCNcjGMZpfQ3CYB0rjjuvgUg"),
        ("Tems", "UCWfi5ELXGAe-DCA6cOP3aNw"),
        ("Rema", "UCHGF6zfD2gwLuke95X3CKFQ"),
        ("Asake", "UCU9R50wyBPKpdbcIScAyZig"),
        ("Ayra Starr", "UCK7oVw_ftuBIwiipiBf7ogA"),
        ("Tiwa Savage", "UCcBJoRL_QYkPlJaMhAUahvA"),
        ("Omah Lay", "UCSUVM9Ygr6AI5Eje5BnFhtw"),
        ("Fireboy DML", "UCvrQdOvBRW-ycBQ32o1BCQw"),
    ],
    "J-Pop": [
        ("Kenshi Yonezu", "UCUCeZaZeJbEYAAzvMgrKOPQ"),
        ("YOASOBI", "UCvpredjG93ifbCP1Y77JyFA"),
        ("Official HIGE DANdism", "UC3vg17IZ1IV73xx069jG44w"),
        ("King Gnu", "UCkB8HnJSDSJ2hkLQFUc-YrQ"),
        ("Ado", "UCln9P4Qm3-EAY4aiEPmRwEA"),
        ("LiSA", "UCm9XuXWofRSxSqX3imN0q9A"),
        ("Perfume", "UCxOjoraUPd0Dq9PAyIhC6tQ"),
        ("RADWIMPS", "UCIVqvhyo8ttjYOmMJuhq_YQ"),
        ("Mrs. GREEN APPLE", "UCpFgmZm65yOU5X-hmWkWjuw"),
        ("ONE OK ROCK", "UCzycs8MqvIY4nXWwS-v4J9g"),
        ("Fujii Kaze", "UCNIy6zQyP7SuLEIaiwymfUA"),
    ],
    "Salsa": [
        ("Marc Anthony", "UCiKsRIULyLr783nNZm-JlAQ"),
        ("Celia Cruz", "UCIE3H3t0DjJ9QHEMMVCsT9A"),
        ("Willie Colón", "UCSovKKustIAHIHk6jpd9IxA"),
        ("Rubén Blades", "UCvRzNVtjYH8fumDk6FvRiHQ"),
        ("El Gran Combo de Puerto Rico", "UCudqQoeNff0uGMTd6vhDNfQ"),
        ("Gilberto Santa Rosa", "UCCTs64yAtgvbvySwf18aAIg"),
        ("Víctor Manuelle", "UCZxOJC1SGP-CaXEr03kNWVg"),
        ("Oscar D'León", "UCXf1RXltdc-j50jhzk0aAVw"),
        ("Grupo Niche", "UCf8Pbsw8fG4BdePN1R2KhmQ"),
        ("Tito Nieves", "UCQufuLjr-glTst7zocvsg8w"),
        ("Joe Arroyo", "UCn8mJdTon41bha6FS5CnS9g"),
    ],
    "Singer-Songwriter": [
        ("John Mayer", "UC9KhB07HSEtWISy_LFWwHzw"),
        ("Jack Johnson", "UCGtoY2YzP_nP54-aRK8s7VA"),
        ("Hozier", "UCwAam3W_VLfb6mEKPW2nDFg"),
        ("Damien Rice", "UCebTyz5lDoxp9OHGmGPe4Zg"),
        ("Tracy Chapman", "UC-rX_f6vueiRY0E75gBNQ0g"),
        ("James Taylor", "UCrbbXRLgMFYYCHPpregTcyQ"),
        ("Carole King", "UCaS5rLrxc6pQDWwpWyQyXUg"),
        ("Leonard Cohen", "UCXWB-kykEYmLveG3Sw0LPOA"),
        ("Cat Stevens", "UCo0pv-U5bTDLlfdROjmCCGg"),
        ("Passenger", "UCFHtCB_FWXQ8GpjgfYcD8-g"),
        ("Lewis Capaldi", "UCveFkLdSOUsGwMJEgedO9dQ"),
        ("Ben Howard", "UC7P46taO0CdI8Gy44P1X2yA"),
    ],
    "Trap": [
        ("Future", "UCFNosi99Sp0_eLilBiXmmXA"),
        ("Travis Scott", "UClRx3MMyYUyqOxyEqA5F2nQ"),
        ("Migos", "UC9YcTIQuhwgoOQqYMKYqW9A"),
        ("21 Savage", "UCOjEHmBKwdS7joWpW0VrXkg"),
        ("Young Thug", "UCuwdplPbuTFZj_64d03tSBA"),
        ("Lil Baby", "UCVS88tG_NYgxF6Udnx2815Q"),
        ("Gucci Mane", "UCSugZEYrWbzqIWGD195V-YA"),
        ("2 Chainz", "UCcZzRX_ZDV-Sg04Ir-upxPA"),
        ("Lil Uzi Vert", "UCqwxMqUcL-XC3D9-fTP93Mg"),
        ("Playboi Carti", "UC652oRUvX1onwrrZ8ADJRPw"),
        ("Metro Boomin", "UCKC11MOR51CLg4JpYj8jb4g"),
        ("Gunna", "UCAkIMkEaa9sZmjcy7mfd5lQ"),
    ],
    "New Age": [
        ("Enya", "UCNIlkuT0DYEc8aFbv3YcvdQ"),
        ("Yanni", "UCb5P0iSygqwPZF7zBxMdi4w"),
        ("Kitarō", "UChFj2_9zQZU9mlg8TTTkyOw"),
        ("Vangelis", "UCWWBOAJrQd7UqNundwjGb-w"),
        ("Enigma", "UC8XZBbAN0qyfOjghG89236A"),
        ("Deep Forest", "UCSCnPN56BRVSDBacr5oeTag"),
        ("Loreena McKennitt", "UCO2gpkWwgxeGvlg5ThMB87w"),
        ("Jean-Michel Jarre", "UCYF-QxHI0RAuQqNzZpvvPBw"),
        ("Secret Garden", "UCvPj1qA4lPWAud3Hnrs5YTA"),
        ("Mike Oldfield", "UCPGQ0FBCid4pZVoFaxxlDPg"),
    ],
}


def main() -> None:
    mismatch = set(CURATED_ARTISTS) ^ set(MUSIC_GENRES)
    if mismatch:
        raise SystemExit(f"CURATED_ARTISTS keys don't match MUSIC_GENRES: {sorted(mismatch)}")

    with SessionLocal() as db:
        for genre in MUSIC_GENRES:
            curated = CURATED_ARTISTS[genre]
            # The avatar URL is part of what's compared, not just the curated
            # pair: a genre whose rows were seeded before the profiles file
            # covered them (or before it was regenerated) has the right
            # artists already and would otherwise be skipped forever, leaving
            # the wizard showing them without avatars.
            existing = [
                (row.artist_name, row.channel_id, row.thumbnail_url)
                for row in db.query(GenreArtist)
                .filter(GenreArtist.genre == genre)
                .order_by(GenreArtist.id)
            ]
            desired = [
                (artist_name, channel_id, PROFILES.get(channel_id, (None, None))[1])
                for artist_name, channel_id in curated
            ]
            if existing == desired:
                print(f"{genre}: {len(curated)} rows already match, skipped")
                continue
            deleted = db.query(GenreArtist).filter(GenreArtist.genre == genre).delete()
            for artist_name, channel_id in curated:
                db.add(build_row(genre, artist_name, channel_id, PROFILES.get(channel_id)))
            # One short transaction per genre: the live app shares this
            # SQLite file (WAL), so don't hold a single giant write txn.
            db.commit()
            print(f"{genre}: -{deleted} +{len(curated)}")


if __name__ == "__main__":
    main()
