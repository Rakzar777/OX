import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from aiogram import Bot
from aiogram.types import Update

from app.bot import build_dispatcher
from app.service import BusinessError
from app.web import create_app
from conftest import paid
from test_bot import TelegramStub


CATALOG = Path(__file__).resolve().parents[1] / "events.json"
DECEMBER = "ox-december-2026"


@pytest.mark.asyncio
async def test_archived_event_hidden_and_old_confirmation_cannot_redeem(service):
    await paid(service, "standard")
    ticket = service.tickets(101)[0]
    original = service.db.one("SELECT * FROM tickets WHERE id=?", (ticket["id"],))
    original_order = service.order(ticket["order_id"])
    app = create_app(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        page = await client.get("/t/" + ticket["token"] + "?confirm=true")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

        service.db.seed(CATALOG)
        service.now = lambda: datetime.fromisoformat("2026-11-20T12:00:00+03:00")
        assert [event["id"] for event in service.events()] == [DECEMBER]
        assert service.tickets(101) == []
        with pytest.raises(BusinessError, match="Мероприятие завершилось"):
            service.redeem(ticket["token"])

        response = await client.post(
            "/t/" + ticket["token"] + "/redeem", data={"csrf": csrf}
        )
        assert response.status_code == 303
        page = await client.get("/t/" + ticket["token"] + "?confirm=true")
        assert page.status_code == 200
        assert "Мероприятие завершилось" in page.text
        assert "O.X. HALLOWEEN NIGHT" in page.text
        assert '<form action=' not in page.text
        assert "Отметить использованным" not in page.text

    assert service.db.one("SELECT * FROM tickets WHERE id=?", (ticket["id"],)) == original
    assert service.order(ticket["order_id"]) == original_order


@pytest.mark.asyncio
@pytest.mark.parametrize("tariff,amount,quantity", [("company", 390000, 6), ("sponsor", 399900, 1)])
async def test_new_event_payment_delivery_and_admission_are_separate(
    service, tariff, amount, quantity
):
    await paid(service, "standard")
    old_ticket = service.tickets(101)[0]
    service.db.seed(CATALOG)
    service.now = lambda: datetime.fromisoformat("2026-11-20T12:00:00+03:00")
    order_id = "december-" + tariff
    service.create_order(
        order_id, 101, DECEMBER, tariff, "buyer@example.com", "Покупатель", "+79991234567"
    )
    await service.pay(order_id, 101)
    payment = service.db.one("SELECT * FROM payments WHERE order_id=?", (order_id,))
    payload = json.loads(payment["payload"])
    assert payload["metadata"]["event_id"] == DECEMBER
    assert payload["metadata"]["quantity"] == str(quantity)
    assert payload["amount"]["value"] == f"{amount // 100}.{amount % 100:02d}"
    assert not service.tickets(101)
    service.provider.payments[payment["id"]].update(
        status="succeeded", paid=True, captured_at=service.now().isoformat()
    )
    await service.payment_webhook(payment["id"])
    await service.payment_webhook(payment["id"])
    tickets = service.tickets(101)
    assert len(tickets) == quantity
    assert len({ticket["token"] for ticket in tickets}) == quantity
    assert {ticket["order_id"] for ticket in tickets} == {order_id}

    session = TelegramStub()
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", session=session)
    dispatcher = build_dispatcher(service)
    try:
        update = {
            "update_id": 1,
            "callback_query": {
                "id": "1",
                "from": {"id": 101, "first_name": "Покупатель", "is_bot": False},
                "chat_instance": "event-admission-test",
                "data": "tickets",
                "message": {
                    "message_id": 1, "date": datetime.now(timezone.utc),
                    "chat": {"id": 101, "type": "private"}, "text": "Мои билеты",
                },
            },
        }
        await dispatcher.feed_update(bot, Update.model_validate(update))
        photos = [call for call in session.calls if call.__api_method__ == "sendPhoto"]
        assert len(photos) == quantity
        assert all("JINGLE BOOBS" in photo.caption for photo in photos)
        assert all(len(photo.caption) <= 1024 for photo in photos)
    finally:
        await dispatcher.storage.close()
        await bot.session.close()

    service.redeem(tickets[0]["token"])
    assert service.ticket(tickets[0]["token"])["status"] == "used"
    with pytest.raises(BusinessError, match="Мероприятие завершилось"):
        service.redeem(old_ticket["token"])
    assert service.ticket(old_ticket["token"])["status"] == "active"


@pytest.mark.asyncio
async def test_retired_preorder_preserves_paid_tickets_and_blocks_unpaid_carts(service, tmp_path):
    catalog = json.loads(CATALOG.read_text(encoding="utf-8-sig"))
    previous = copy.deepcopy(catalog)
    december = next(event for event in previous if event["id"] == DECEMBER)
    december["tariffs"] = [{
        "id": "preorder", "name": "Предзаказанный", "price": 690, "quantity": 1,
        "description": "Предзаказ",
    }]
    previous_path = tmp_path / "before.json"
    previous_path.write_text(json.dumps(previous), encoding="utf-8")
    service.db.seed(previous_path)
    service.now = lambda: datetime.fromisoformat("2026-11-20T12:00:00+03:00")
    for order_id in ("purchased-preorder", "unpaid-preorder"):
        service.create_order(
            order_id, 101, DECEMBER, "preorder", "buyer@example.com", "Покупатель", "+79991234567"
        )
    await service.pay("purchased-preorder", 101)
    payment = service.db.one("SELECT * FROM payments WHERE order_id='purchased-preorder'")
    service.provider.payments[payment["id"]].update(
        status="succeeded", paid=True, captured_at=service.now().isoformat()
    )
    service.db.seed(CATALOG)
    # A payment started before the tariff was removed may still complete.
    await service.payment_webhook(payment["id"])
    tickets = service.tickets(101)
    assert len(tickets) == 1
    assert service.order("purchased-preorder")["amount"] == 69000
    assert json.loads(tickets[0]["snapshot"])["tariff"]["name"] == "Предзаказанный"
    with pytest.raises(BusinessError, match="больше не продаётся"):
        await service.pay("unpaid-preorder", 101)
    assert len(service.provider.payment_keys) == 1
    service.redeem(tickets[0]["token"])
    assert service.ticket(tickets[0]["token"])["status"] == "used"
