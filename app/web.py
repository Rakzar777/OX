import hashlib
import hmac
import re
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .provider import ProviderError
from .service import BusinessError, MSK, dt
from .volunteer import SESSION_COOKIE, SESSION_SECONDS, VolunteerAuth

ROOT = Path(__file__).parent


class ProviderObject(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9-]{1,64}$")


class Notification(BaseModel):
    type: str
    event: str
    object: ProviderObject


def create_app(service, lifespan=None):
    app = FastAPI(title="O.X. Tickets", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "templates")
    csrf_key = secrets.token_bytes(32)
    auth = VolunteerAuth(service.settings)
    volunteer_csrf_cookie = "__Host-ox_volunteer_csrf"

    def csrf_pair(scope, nonce=None):
        nonce = nonce or secrets.token_urlsafe(24)
        signature = hmac.new(csrf_key, (scope + ":" + nonce).encode(), hashlib.sha256).hexdigest()
        return nonce, nonce + "." + signature

    def verify_csrf(request, value, scope, cookie_name):
        nonce = request.cookies.get(cookie_name, "")
        _, expected = csrf_pair(scope, nonce)
        if not nonce or not hmac.compare_digest(value.encode(), expected.encode()):
            raise HTTPException(403, "Обнови страницу и повтори действие")
        origin = request.headers.get("origin")
        # Privacy settings and cached pages can submit an opaque Origin.
        # The signed CSRF token and its HttpOnly cookie were verified above.
        if origin and origin != "null" and origin != service.settings.public_base_url:
            raise HTTPException(403)

    async def form_values(request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(413)
        try:
            return parse_qs(body.decode("utf-8"), max_num_fields=8)
        except (UnicodeError, ValueError):
            raise HTTPException(400) from None

    def next_page(value):
        if re.fullmatch(r"/t/[a-zA-Z0-9_-]{40,48}", value or ""):
            return value
        return "/volunteer"

    def volunteer_page(request, next_path="/volunteer", error=None, status_code=200):
        authenticated = auth.authenticated(request)
        nonce, csrf = csrf_pair("volunteer", request.cookies.get(volunteer_csrf_cookie))
        response = templates.TemplateResponse(request=request, name="volunteer.html", context={
            "configured": auth.configured, "volunteer": authenticated, "csrf": csrf,
            "next_path": next_path, "error": error,
        }, status_code=status_code)
        response.set_cookie(volunteer_csrf_cookie, nonce, httponly=True, secure=True,
                            samesite="strict", max_age=900, path="/")
        return response

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "same-origin",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "X-Robots-Tag": "noindex, nofollow",
            "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"})
        return response

    @app.get("/health")
    async def health():
        service.db.one("SELECT 1")
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    @app.get("/payment-return", response_class=HTMLResponse)
    async def returned(request: Request):
        return templates.TemplateResponse(request=request, name="return.html", context={
            "bot_url": "https://t.me/" + service.settings.bot_username + "?start=payment",
            "manager": service.settings.manager,
        })

    @app.get("/volunteer", response_class=HTMLResponse)
    @app.get("/volunteer/login", response_class=HTMLResponse)
    async def volunteer_login(request: Request, next: str = "/volunteer"):
        target = next_page(next)
        if not auth.configured:
            return volunteer_page(request, target, "Вход для волонтёров ещё не настроен.", 503)
        if auth.authenticated(request) and target != "/volunteer":
            return RedirectResponse(target, status_code=303)
        return volunteer_page(request, target)

    @app.post("/volunteer/login")
    async def volunteer_sign_in(request: Request):
        if not auth.configured:
            raise HTTPException(503, "Вход для волонтёров ещё не настроен")
        values = await form_values(request)
        target = next_page(values.get("next", [""])[0])
        try:
            verify_csrf(request, values.get("csrf", [""])[0], "volunteer", volunteer_csrf_cookie)
        except HTTPException as error:
            if error.status_code != 403:
                raise
            return volunteer_page(
                request, target, "Форма входа обновлена. Введи логин и пароль ещё раз.", 403
            )
        username = values.get("username", [""])[0].strip()
        password = values.get("password", [""])[0]
        if not await run_in_threadpool(auth.verify_credentials, username, password):
            return volunteer_page(request, target, "Неверный логин или пароль.", 401)
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(SESSION_COOKIE, auth.issue_session(), httponly=True, secure=True,
                            samesite="lax", max_age=SESSION_SECONDS, path="/")
        return response

    @app.post("/volunteer/logout")
    async def volunteer_sign_out(request: Request):
        values = await form_values(request)
        verify_csrf(request, values.get("csrf", [""])[0], "volunteer", volunteer_csrf_cookie)
        response = RedirectResponse("/volunteer/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, httponly=True, secure=True, samesite="lax", path="/")
        return response

    @app.get("/t/{token}", response_class=HTMLResponse)
    async def ticket_page(request: Request, token: str, confirm: bool = False):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{40,48}", token):
            raise HTTPException(404, "Билет не найден")
        ticket = service.ticket(token)
        if not ticket:
            return templates.TemplateResponse(request=request, name="ticket.html", context={"ticket": None}, status_code=404)
        volunteer = auth.authenticated(request)
        if confirm and not volunteer:
            return RedirectResponse("/volunteer/login?" + urlencode({"next": "/t/" + token}), status_code=303)
        import json
        snapshot = json.loads(ticket["snapshot"])
        status = ticket["status"]
        if status == "active" and ticket["order_status"] == "refund_pending":
            status = "refund_pending"
        elif status == "active" and not ticket["admission_enabled"]:
            status = "archived"
        labels = {"active": "Билет активен", "used": "Билет использован", "refunded": "Билет возвращён",
                  "canceled": "Билет недействителен", "refund_pending": "Возврат в обработке",
                  "archived": "Мероприятие завершилось"}
        nonce, csrf = csrf_pair(token)
        logout_nonce, logout_csrf = csrf_pair("volunteer", request.cookies.get(volunteer_csrf_cookie))
        response = templates.TemplateResponse(request=request, name="ticket.html", context={
            "ticket": ticket, "event": snapshot["event"], "tariff": snapshot["tariff"],
            "status": status, "label": labels.get(status, "Билет недействителен"),
            "date": dt(snapshot["event"]["starts_at"]).astimezone(MSK).strftime("%d.%m.%Y · %H:%M"),
            "used_at": datetime.fromtimestamp(ticket["used_at"], MSK).strftime("%d.%m.%Y · %H:%M:%S") if ticket["used_at"] else None,
            "confirm": confirm, "csrf": csrf, "volunteer": volunteer, "logout_csrf": logout_csrf,
        })
        if volunteer:
            response.set_cookie("ticket_csrf", nonce, httponly=True, secure=service.settings.public_base_url.startswith("https://"),
                                samesite="strict", max_age=900, path="/t/" + token)
            response.set_cookie(volunteer_csrf_cookie, logout_nonce, httponly=True, secure=True,
                                samesite="strict", max_age=900, path="/")
        return response

    @app.post("/t/{token}/redeem")
    async def redeem(request: Request, token: str):
        if not auth.authenticated(request):
            raise HTTPException(401, "Войди как волонтёр для проверки билета")
        values = await form_values(request)
        verify_csrf(request, values.get("csrf", [""])[0], token, "ticket_csrf")
        try:
            service.redeem(token)
        except BusinessError:
            pass  # Render the final, authoritative state, including competing scans.
        return RedirectResponse("/t/" + token, status_code=303)

    @app.post("/webhooks/yookassa")
    async def yookassa(notification: Notification):
        if not service.settings.payments_enabled:
            raise HTTPException(503, "Приём платежей ещё не включён")
        if notification.type != "notification":
            raise HTTPException(400)
        try:
            if notification.event in ("payment.succeeded", "payment.canceled"):
                await service.payment_webhook(notification.object.id)
            elif notification.event == "refund.succeeded":
                await service.refund_webhook(notification.object.id)
        except ProviderError:
            raise HTTPException(503, "Повторите уведомление") from None
        except (ValueError, KeyError):
            raise HTTPException(400, "Несоответствие платежа") from None
        return {"ok": True}

    return app
