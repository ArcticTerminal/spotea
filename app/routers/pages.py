from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.content_query import followed_feeds
from app.deps import get_current_profile, get_db, require_login
from app.genres import MUSIC_GENRES, PODCAST_CATEGORIES
from app.interests import parse_interests
from app.models import User
from app.page_context import (
    downloads_context,
    home_context,
    home_shelf_items,
    library_context,
    queue_thumbnail_caching,
)
from app.templating import templates

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    background_tasks: BackgroundTasks,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """index.html is the whole app — Home, Library, Explore and Settings are
    tab panels in one document (see its inline head script), so this single
    route builds the context for all of them at once.

    Each region's context comes from app/page_context.py, shared with the
    fragment endpoints (routers/partials.py) that re-render that same region
    later. The full page and a refresh of one part of it therefore can't
    disagree about what it contains."""
    home = home_context(db, profile.id)
    queue_thumbnail_caching(background_tasks, home_shelf_items(home))
    interests = parse_interests(profile.interests)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "audio_quality": profile.audio_quality,
            # Which profile is active is otherwise invisible: the topbar
            # button and mobile menu row both shipped with static placeholder
            # text ("Profile" / "Switch profile") that nothing ever replaced.
            # With more than one profile there was no way to tell which one
            # you were on without opening the switcher itself. See
            # profiles.js's renameProfile for the one case a reload doesn't
            # already cover (renaming the profile you're currently on).
            "profile_name": profile.name,
            # Labels Settings' Account group, so the rows that reach past the
            # active profile say whose login they belong to. The login is
            # otherwise never shown anywhere in the app after registration.
            "account_email": profile.account.email,
            "feed_refresh_interval_minutes": profile.account.feed_refresh_interval_minutes,
            # Server-rendered rather than fetched by home/settings.js on boot:
            # the interest chips are part of the Settings panel's first paint,
            # and filling them in afterwards flashes an empty editor on every
            # load. Explore's recommendations are the opposite case — they can
            # cost a YouTube round trip, so they stay a deliberate fetch.
            "interests": interests,
            # Drives the onboarding wizard's auto-open (see home/onboarding.js):
            # a profile with neither an interest nor a followed channel has
            # nothing for Explore's recommendations or the library to work
            # from, so it's shown again on every such load rather than tracked
            # with its own "seen it" flag — closing it without adding either
            # is treated the same as never having seen it.
            "needs_onboarding": not interests and followed_feeds(db, user_id=profile.id).first() is None,
            # The onboarding wizard's chip lists — one Python list per kind
            # that both the template loops and the curated seed scripts
            # (scripts/seed_music_artists.py, scripts/seed_podcast_channels.py)
            # key off of, so the chips and the suggestion cache can't drift.
            "music_genres": MUSIC_GENRES,
            "podcast_categories": PODCAST_CATEGORIES,
            **home,
            **library_context(db, profile.id),
            **downloads_context(db, profile.id),
        },
    )


# Channel, the four pinned playlists, and the player all moved in-page (see
# home/detail.js, home/overlay.js) — index.html's hash router handles them
# now. These redirects exist only so a link or bookmark from before that
# change still lands somewhere real.
@router.get("/favorites")
def favorites_redirect() -> RedirectResponse:
    return RedirectResponse("/#favorites")


@router.get("/saved")
def saved_redirect() -> RedirectResponse:
    return RedirectResponse("/#saved")


@router.get("/new-uploads")
def new_uploads_redirect() -> RedirectResponse:
    return RedirectResponse("/#new-uploads")


@router.get("/recently-played")
def recently_played_redirect() -> RedirectResponse:
    return RedirectResponse("/#recently-played")


@router.get("/channel/{feed_id}")
def channel_redirect(feed_id: int) -> RedirectResponse:
    return RedirectResponse(f"/#channel/{feed_id}")


@router.get("/player/{content_id}")
def player_redirect(content_id: int) -> RedirectResponse:
    return RedirectResponse(f"/#player/{content_id}")
