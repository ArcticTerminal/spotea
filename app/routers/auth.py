from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import SESSION_KEY, check_password

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get(SESSION_KEY):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, password: str = Form(...)):
    if not check_password(password):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Incorrect password"}, status_code=401
        )
    request.session[SESSION_KEY] = True
    return RedirectResponse(url="/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
