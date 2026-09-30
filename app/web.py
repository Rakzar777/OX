import hashlib
import hmac
import re
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .provider import ProviderError
from .service import BusinessError, MSK, dt

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

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
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

    @app.get("/t/{token}", response_class=HTMLResponse)
    async def ticket_page(request: Request, token: str, confirm: bool = False):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{40,48}", token):
            raise HTTPException(404, "Билет не найден")
        ticket = service.ticket(token)
        if not ticket:
            return templates.TemplateResponse(request=request, name="ticket.html", context={"ticket": None}, status_code=404)
        import json
        snapshot = json.loads(ticket["snapshot"])
        status = ticket["status"]
        if status == "active" and ticket["order_status"] == "refund_pending":
            status = "refund_pending"
        labels = {"active": "Билет активен", "used": "Билет использован", "refunded": "Билет возвращён",
                  "canceled": "Билет недействителен", "refund_pending": "Возврат в обработке"}
        nonce = secrets.token_urlsafe(24)
        signature = hmac.new(csrf_key, (token + ":" + nonce).encode(), hashlib.sha256).hexdigest()
        response = templates.TemplateResponse(request=request, name="ticket.html", context={
            "ticket": ticket, "event": snapshot["event"], "tariff": snapshot["tariff"],
            "status": status, "label": labels.get(status, "Билет недействителен"),
            "date": dt(snapshot["event"]["starts_at"]).astimezone(MSK).strftime("%d.%m.%Y · %H:%M"),
            "used_at": datetime.fromtimestamp(ticket["used_at"], MSK).strftime("%d.%m.%Y · %H:%M:%S") if ticket["used_at"] else None,
            "confirm": confirm, "csrf": nonce + "." + signature,
        })
        response.set_cookie("ticket_csrf", nonce, httponly=True, secure=service.settings.public_base_url.startswith("https://"),
                            samesite="strict", max_age=900, path="/t/" + token)
        return response

    @app.post("/t/{token}/redeem")
    async def redeem(request: Request, token: str):
        # Form is deliberately POST-only; link previews / QR scanners cannot consume tickets.
        from urllib.parse import parse_qs
        body = await request.body()
        if len(body) > 2048:
            raise HTTPException(400)
        value = parse_qs(body.decode()).get("csrf", [""])[0]
        nonce = request.cookies.get("ticket_csrf", "")
        expected = nonce + "." + hmac.new(csrf_key, (token + ":" + nonce).encode(), hashlib.sha256).hexdigest()
        if not nonce or not hmac.compare_digest(value, expected):
            raise HTTPException(403, "Обнови страницу билета и повтори действие")
        origin = request.headers.get("origin")
        if origin and origin != service.settings.public_base_url:
            raise HTTPException(403)
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
