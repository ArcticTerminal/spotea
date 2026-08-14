from fastapi.templating import Jinja2Templates

from app.formatting import format_duration, format_size

# One Jinja environment for the whole app. There used to be two — one in
# routers/pages.py with these filters registered, one in routers/auth.py
# without them — so which templates could use `| duration` / `| filesize`
# depended on which router happened to render them. Nothing caught that,
# because the only templates auth.py renders (login, register) don't use
# either filter; a page moved between routers, or a new filter added to the
# "wrong" instance, would have failed at render time with no warning.
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["duration"] = format_duration
templates.env.filters["filesize"] = format_size
