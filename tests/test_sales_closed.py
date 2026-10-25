import json
from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.types import Update

from app.bot import build_dispatcher, menu
from app.service import BusinessError
from conftest import paid
from test_bot import TelegramStub


NOTICE = "Онлайн-продажа билетов закрыта. Билеты можно приобрести на входе в клуб за 1 850 ₽."


def close_sales(service):
    event = service.event("halloween-2026")
    event.update(enabled=False, sales_closed_notice=NOTICE)
    with service.db.transaction() as con:
        con.execute("UPDATE events SET enabled=0,data=? WHERE id=?", (json.dumps(event), event["id"]))


@pytest.mark.asyncio
async def test_closed_sales_block_new_and_stale_orders_but_preserve_admission(service):
    await paid(service, "standard")
    tickets = service.tickets(101)
    service.create_order("old-cart", 101, "halloween-2026", "standard", "a@example.com", "Имя", "+79991234567")
    close_sales(service)
    with pytest.raises(BusinessError, match="1 850"):
        service.create_order("new-cart", 101, "halloween-2026", "standard", "a@example.com", "Имя", "+79991234567")
    with pytest.raises(BusinessError, match="1 850"):
        await service.pay("old-cart", 101)
    assert len(service.provider.payment_keys) == 1
    assert not service.db.one("SELECT * FROM orders WHERE id='new-cart'")
    assert service.tickets(101) == tickets
    service.redeem(tickets[0]["token"])
    assert service.ticket(tickets[0]["token"])["status"] == "used"


@pytest.mark.asyncio
async def test_closed_notice_on_start_menu_and_all_old_purchase_buttons(service):
    await paid(service, "standard")
    service.create_order("old-cart", 101, "halloween-2026", "standard", "a@example.com", "Имя", "+79991234567")
    close_sales(service)
    assert all(button.callback_data != "buy" for row in menu(service).inline_keyboard for button in row)
    session = TelegramStub()
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", session=session)
    dp = build_dispatcher(service)
    user = {"id": 101, "is_bot": False, "first_name": "Имя"}
    actions = ["/start", "home", "buy", "event:halloween-2026", "tariff:halloween-2026:standard", "checkout", "pay:old-cart", "tickets"]
    for counter, action in enumerate(actions, 1):
        message = {"message_id": counter, "date": datetime.now(timezone.utc),
                   "chat": {"id": 101, "type": "private"}, "from": user, "text": action}
        update = {"update_id": counter}
        if action == "/start":
            update["message"] = message
        else:
            update["callback_query"] = {"id": str(counter), "from": user, "chat_instance": "test", "data": action, "message": message}
        await dp.feed_update(bot, Update.model_validate(update))
        if action != "tickets":
            assert NOTICE in session.calls[-1].text
    assert len([call for call in session.calls if call.__api_method__ == "sendPhoto"]) == 1
    assert len(service.provider.payment_keys) == 1
    await dp.storage.close()
    await bot.session.close()
