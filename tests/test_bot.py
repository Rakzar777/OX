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
