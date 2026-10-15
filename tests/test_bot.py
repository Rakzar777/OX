import json
from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Update, Message

from app.bot import build_dispatcher, notifications


class TelegramStub(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self,bot,method,timeout=None):
        self.calls.append(method)
        if method.__api_method__ == "answerCallbackQuery":
            return True
        return Message(message_id=len(self.calls),date=datetime.now(timezone.utc),
            chat={"id":101,"type":"private"},text=getattr(method,"text",None))

    async def stream_content(self,url,**kwargs):
        yield b""


@pytest.mark.asyncio
async def test_complete_dialog_purchase_and_delivery(service):
    session = TelegramStub()
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",session=session)
    dp = build_dispatcher(service)
    counter = 0
    async def feed(text=None,callback=None):
        nonlocal counter
        counter += 1
        user = {"id":101,"is_bot":False,"first_name":"Иван"}
        message={"message_id":counter,"date":datetime.now(timezone.utc),"chat":{"id":101,"type":"private"},"from":user,"text":text or "Кнопка"}
        update={"update_id":counter}
        if callback:
            update["callback_query"]={"id":str(counter),"from":user,"chat_instance":"test","message":message,"data":callback}
        else:
            update["message"]=message
        await dp.feed_update(bot,Update.model_validate(update))

    await feed(text="/start")
    await feed(callback="buy")
    await feed(callback="tariff:halloween-2026:company")
    await feed(callback="checkout")
    await feed(text="bad email")
    assert "Проверь email" in session.calls[-1].text
    await feed(text="buyer@example.com")
    await feed(text="Иван Иванов")
    await feed(text="8 (999) 123-45-67")
    order=service.db.one("SELECT * FROM orders")
    assert order and order["quantity"] == 6 and order["phone"] == "+79991234567"
    assert "Проверь данные" in session.calls[-1].text
    await feed(callback="pay:"+order["id"])
    assert "ЮKassa" in session.calls[-1].reply_markup.inline_keyboard[0][0].text
    payment=service.db.one("SELECT * FROM payments")
    service.provider.payments[payment["id"]].update(status="succeeded",paid=True,captured_at=service.provider.captured_at)
    await service.payment_webhook(payment["id"])
    await notifications(bot,service)
    photos=[m for m in session.calls if m.__api_method__ == "sendPhoto"]
    assert len(photos)==6
    assert all(m.photo.data.startswith(b'\x89PNG') for m in photos)
    await notifications(bot,service)
    assert len([m for m in session.calls if m.__api_method__ == "sendPhoto"])==6
    await feed(callback="refund:"+order["id"])
    assert "6 билетов, 3900" in session.calls[-1].text
    await feed(callback="refund_yes:"+order["id"])
    assert service.order(order["id"])["status"]=="refunded"
    await dp.storage.close()
    await bot.session.close()

@pytest.mark.asyncio
async def test_two_separate_orders_have_distinct_ticket_captions(service):
    from conftest import paid
    from app.bot import send_ticket
    await paid(service, tariff='standard', order_id='a'*32)
    await paid(service, tariff='standard', order_id='b'*32)
    session = TelegramStub()
    bot = Bot('123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=session)
    tickets = service.tickets(101)
    assert len(tickets) == 2
    for ticket in tickets:
        await send_ticket(bot, service, 101, ticket)
    photos = [m for m in session.calls if m.__api_method__ == 'sendPhoto']
    assert len(photos) == 2
    assert photos[0].caption != photos[1].caption
    assert photos[0].photo.data != photos[1].photo.data
    for photo, ticket in zip(photos, tickets):
        assert ticket['id'][:8].upper() in photo.caption
        assert ticket['full_name'] in photo.caption
        assert len(photo.caption) <= 1024
    await bot.session.close()


@pytest.mark.asyncio
async def test_cancelled_attempt_does_not_claim_successful_ticket_is_lost(service):
    from conftest import paid
    await paid(service, "standard")
    service.create_order("b" * 32, 101, "halloween-2026", "vip", "a@example.com", "Имя", "+79991234567")
    await service.pay("b" * 32, 101)
    payment = service.db.one("SELECT * FROM payments WHERE order_id=?", ("b" * 32,))
    service.provider.payments[payment["id"]]["status"] = "canceled"
    await service.payment_webhook(payment["id"])
    session = TelegramStub()
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", session=session)
    await notifications(bot, service)
    texts = [m.text for m in session.calls if m.__api_method__ == "sendMessage"]
    assert any("BBBBBBBB" in t and "1490" in t and "только к этой попытке" in t for t in texts)
    assert len(service.tickets(101)) == 1
    await bot.session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("button", ["check:", "tickets"])
async def test_user_buttons_recover_success_without_webhook(service, button):
    oid = "f" * 32
    service.create_order(oid, 101, "halloween-2026", "vip", "a@example.com", "Имя", "+79991234567")
    await service.pay(oid, 101)
    payment = service.db.one("SELECT * FROM payments WHERE order_id=?", (oid,))
    service.provider.payments[payment["id"]].update(status="succeeded", paid=True,
        captured_at=service.provider.captured_at)
    session = TelegramStub()
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", session=session)
    dp = build_dispatcher(service)
    update = Update.model_validate({"update_id": 1, "callback_query": {
        "id": "1", "from": {"id": 101, "is_bot": False, "first_name": "Имя"},
        "chat_instance": "test", "data": button + oid if button == "check:" else button,
        "message": {"message_id": 2, "date": datetime.now(timezone.utc),
                    "chat": {"id": 101, "type": "private"}, "text": "Оплата"}}})
    await dp.feed_update(bot, update)
    assert service.order(oid)["status"] == "paid"
    assert len([m for m in session.calls if m.__api_method__ == "sendPhoto"]) == 1
    assert len(service.provider.payment_keys) == 1
    await dp.storage.close()
    await bot.session.close()
