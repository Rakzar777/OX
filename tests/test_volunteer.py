import re
import time

import httpx
import pytest

from app.volunteer import SESSION_COOKIE, SESSION_SECONDS, hash_password
from app.web import create_app
from conftest import login_volunteer, paid


def csrf_from(page):
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


@pytest.mark.asyncio
async def test_guest_cannot_confirm_or_redeem_even_with_a_volunteers_csrf(service, volunteer_settings):
    await paid(service, "standard")
    path = "/t/" + service.tickets(101)[0]["token"]
    app = create_app(service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url=volunteer_settings.public_base_url) as volunteer:
        await login_volunteer(volunteer, path)
        confirmation = await volunteer.get(path + "?confirm=true")
        csrf = csrf_from(confirmation)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url=volunteer_settings.public_base_url) as guest:
            page = await guest.get(path)
            assert "Билет активен" in page.text and "Вход для волонтёров" in page.text
            assert "Отметить использованным" not in page.text
            assert (await guest.get(path + "?confirm=true")).status_code == 303
            guest.cookies.set("ticket_csrf", volunteer.cookies.get("ticket_csrf"))
            response = await guest.post(path + "/redeem", data={"csrf": csrf})
            assert response.status_code == 401
    assert service.ticket(path.rsplit("/", 1)[1])["status"] == "active"


@pytest.mark.asyncio
async def test_wrong_password_does_not_create_a_session(service, volunteer_settings):
    app = create_app(service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url=volunteer_settings.public_base_url) as client:
        page = await client.get("/volunteer/login")
        response = await client.post("/volunteer/login", data={
            "csrf": csrf_from(page), "username": "volunteer", "password": "wrong",
        })
        assert response.status_code == 401
        assert "Неверный логин или пароль" in response.text
        assert SESSION_COOKIE not in client.cookies


@pytest.mark.asyncio
async def test_shared_login_survives_new_qr_and_application_restart(service, volunteer_settings):
    await paid(service, "standard", order_id="a" * 32)
    await paid(service, "standard", order_id="b" * 32)
    tickets = service.tickets(101)
    app = create_app(service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url=volunteer_settings.public_base_url) as client:
        signed_in = await login_volunteer(client)
        cookie_header = signed_in.headers["set-cookie"]
        assert "HttpOnly" in cookie_header and "Secure" in cookie_header
        assert "SameSite=lax" in cookie_header and f"Max-Age={SESSION_SECONDS}" in cookie_header
        for ticket in tickets:
            page = await client.get("/t/" + ticket["token"])
            assert "Отметить использованным" in page.text
        cookies = client.cookies
    restarted_app = create_app(service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted_app),
                                 base_url=volunteer_settings.public_base_url, cookies=cookies) as client:
        path = "/t/" + tickets[1]["token"]
        page = await client.get(path + "?confirm=true")
        assert page.status_code == 200
        assert "Да, отметить использованным" in page.text
        response = await client.post(path + "/redeem", data={"csrf": csrf_from(page)})
        assert response.status_code == 303
    assert service.ticket(tickets[0]["token"])["status"] == "active"
    assert service.ticket(tickets[1]["token"])["status"] == "used"


@pytest.mark.asyncio
async def test_logout_blocks_redemption_from_an_already_open_form(service, volunteer_settings):
    await paid(service, "standard")
    ticket = service.tickets(101)[0]
    path = "/t/" + ticket["token"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service)),
                                 base_url=volunteer_settings.public_base_url) as client:
        await login_volunteer(client)
        confirmation = await client.get(path + "?confirm=true")
        page = await client.get("/volunteer")
        assert "Вход выполнен" in page.text
        assert (await client.post("/volunteer/logout", data={"csrf": csrf_from(page)})).status_code == 303
        assert SESSION_COOKIE not in client.cookies
        response = await client.post(path + "/redeem", data={"csrf": csrf_from(confirmation)})
        assert response.status_code == 401
    assert service.ticket(ticket["token"])["status"] == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "forged", "password_rotated"])
async def test_invalid_session_cannot_redeem(service, volunteer_settings, monkeypatch, change):
    await paid(service, "standard")
    path = "/t/" + service.tickets(101)[0]["token"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service)),
                                 base_url=volunteer_settings.public_base_url) as client:
        await login_volunteer(client)
        session = client.cookies.get(SESSION_COOKIE)
        if change == "expired":
            after_expiry = time.time() + SESSION_SECONDS + 1
            monkeypatch.setattr("app.volunteer.time.time", lambda: after_expiry)
        elif change == "forged":
            session = session[:-1] + ("a" if session[-1] != "a" else "b")
        else:
            volunteer_settings.volunteer_password_hash = hash_password("new-private-password")
        response = await client.post(path + "/redeem", headers={"Cookie": SESSION_COOKIE + "=" + session})
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_requires_csrf_and_restricts_return_address(service, volunteer_settings):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service)),
                                 base_url=volunteer_settings.public_base_url) as client:
        response = await client.post("/volunteer/login", data={"username": "volunteer", "password": "wrong"})
        assert response.status_code == 403
        page = await client.get("/volunteer/login")
        response = await client.post("/volunteer/login", headers={"Origin": "https://other.test"}, data={
            "csrf": csrf_from(page), "username": "volunteer", "password": "wrong",
        })
        assert response.status_code == 403
        logged_in = await login_volunteer(client, "//other.test/steal")
        assert logged_in.headers["location"] == "/volunteer"
        oversized = await client.post("/volunteer/login", content=b"a" * 4097)
        assert oversized.status_code == 413


@pytest.mark.asyncio
async def test_missing_credentials_keep_redemption_closed(service):
    await paid(service, "standard")
    path = "/t/" + service.tickets(101)[0]["token"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service)),
                                 base_url="https://tickets.test") as client:
        assert (await client.get("/volunteer/login")).status_code == 503
        assert (await client.post("/volunteer/login")).status_code == 503
        assert (await client.post(path + "/redeem")).status_code == 401
