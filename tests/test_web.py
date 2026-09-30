import re

import httpx
import pytest

from app.web import create_app
from conftest import paid


@pytest.mark.asyncio
async def test_public_page_get_does_not_use_ticket_post_requires_csrf(service):
    await paid(service,"standard")
    token = service.tickets(101)[0]["token"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service)),base_url="http://127.0.0.1:8000") as client:
        page = await client.get("/t/"+token)
        assert page.status_code == 200
        assert "Билет активен" in page.text
        assert "buyer@example.com" not in page.text
        assert "Иван Иванов" not in page.text
        assert service.ticket(token)["status"] == "active"
        assert (await client.post("/t/"+token+"/redeem")).status_code == 403
        page = await client.get("/t/"+token+"?confirm=true")
        csrf = re.search('name="csrf" value="([^"]+)"',page.text).group(1)
        response = await client.post("/t/"+token+"/redeem",data={"csrf":csrf})
        assert response.status_code == 303
        assert service.ticket(token)["status"] == "used"
        assert "Билет использован" in (await client.get("/t/"+token)).text


@pytest.mark.asyncio
async def test_forged_webhook_cannot_issue_ticket(service):
    oid="f"*32
    service.create_order(oid,101,"halloween-2026","standard","a@example.com","Имя","+79991234567")
    await service.pay(oid,101)
    pid=service.db.one("SELECT id FROM payments WHERE order_id=?",(oid,))["id"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service)),base_url="http://test") as client:
        resp=await client.post("/webhooks/yookassa",json={"type":"notification","event":"payment.succeeded","object":{"id":pid,"status":"succeeded","paid":True}})
        assert resp.status_code == 200
        assert not service.tickets(101)
        assert (await client.get("/payment-return")).status_code == 200
        assert not service.tickets(101)


@pytest.mark.asyncio
async def test_wrong_mode_and_bad_ids_rejected(service):
    oid,pid=await paid(service,"standard")
    service.provider.payments[pid]["test"]=False
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service)),base_url="http://test") as client:
        resp=await client.post("/webhooks/yookassa",json={"type":"notification","event":"payment.succeeded","object":{"id":pid}})
        assert resp.status_code == 400
        resp=await client.post("/webhooks/yookassa",json={"type":"notification","event":"payment.succeeded","object":{"id":"../../refunds"}})
        assert resp.status_code == 422
