import asyncio
import html
import io
import json
import logging
import re
import uuid

import qrcode
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, ErrorEvent
from email_validator import EmailNotValidError, validate_email

from .provider import ProviderError
from .service import BusinessError, MSK, dt, rub

log = logging.getLogger(__name__)
esc = html.escape


class Checkout(StatesGroup):
    email = State()
    name = State()
    phone = State()
    review = State()


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, **({"url": target} if target.startswith("https://") else {"callback_data": target}))
        for text, target in row] for row in rows])


def menu(service):
    return keyboard([[('Купить билет', 'buy')], [('Мои билеты', 'tickets')],
        [('Задать вопрос', 'https://t.me/' + service.settings.manager)]])


def event_text(event):
    start = dt(event["starts_at"]).astimezone(MSK).strftime("%d.%m.%Y · %H:%M")
    return f"<b>{esc(event['title'])}</b>\n\n{start} МСК\n{esc(event['venue'])}\n\n{esc(event.get('age', ''))}" + ("\n\n" + esc(event['description']) if event.get('description') else "")


async def send_ticket(bot, service, chat_id, ticket):
    snap = json.loads(ticket["snapshot"])
    qr = qrcode.make(service.settings.public_base_url + "/t/" + ticket["token"])
    stream = io.BytesIO()
    qr.save(stream, format="PNG")
    status = "Возврат в обработке" if ticket["order_status"] == "refund_pending" else "Активен"
    text = (event_text(snap["event"]) + f"\n\n{esc(snap['tariff']['name'])} · билет № {ticket['position']}"
        + f"\nСтатус: {status}\n\nПокажи QR-код волонтёру на входе. Дождись, пока он отсканирует код и отметит билет, затем проходи.\n\nНе нажимай «Отметить использованным» самостоятельно: отменить отметку нельзя, повторный вход недоступен.\n\nБилет можно переслать другу обычным сообщением в Telegram. Один QR-код — один проход.")
    rows = []
    if ticket["order_status"] == "paid":
        rows.append([("Вернуть заказ — 6 билетов" if snap["tariff"]["quantity"] == 6 else "Вернуть билет", "refund:" + ticket["order_id"])])
    rows.append([("Мои билеты", "tickets"), ("Главное меню", "home")])
    await bot.send_photo(chat_id, BufferedInputFile(stream.getvalue(), filename=f"ticket-{ticket['position']}.png"),
                         caption=text, parse_mode="HTML", reply_markup=keyboard(rows))


def build_dispatcher(service):
    router = Router()
    router.message.filter(F.chat.type == "private")
    router.callback_query.filter(F.message.chat.type == "private")
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dp.include_router(router)

    async def show_event(message, event_id):
        event = service.event(event_id)
        if not event:
            raise BusinessError("Продажи завершены.")
        rows = [[(t["name"] + " — " + rub(int(t["price"] * 100)), "tariff:" + event_id + ":" + t["id"])] for t in event["tariffs"]]
        rows += [[("Назад", "home")]]
        await message.answer(event_text(event), parse_mode="HTML", reply_markup=keyboard(rows))

    async def show_tickets(message, user_id):
        tickets = service.tickets(user_id)
        if not tickets:
            await message.answer("У тебя пока нет активных билетов.", reply_markup=menu(service))
            return
        await message.answer("Твои активные билеты:")
        for ticket in tickets:
            await send_ticket(message.bot,service,user_id,ticket)

    @router.message(CommandStart())
    async def start(message, state: FSMContext):
        await state.clear()
        if message.text.split(maxsplit=1)[-1] == "payment":
            await show_tickets(message, message.from_user.id)
            pending = service.db.one("SELECT id FROM orders WHERE user_id=? AND status='pending'", (message.from_user.id,))
            if pending:
                await message.answer("Проверяем оплату. Обычно это занимает меньше минуты. Билеты появятся в «Моих билетах».", reply_markup=menu(service))
            return
        await message.answer("Хочешь на вечеринку в Санкт-Петербурге? Ты по адресу.\n\n"
            "TG вечеринки\nhttps://t.me/OX_PARTY\n\n"
            "Группа в VK\nhttps://vk.com/ohhhparty", reply_markup=menu(service))

    @router.callback_query(F.data == "home")
    async def home(cb, state: FSMContext):
        await cb.answer()
        await state.clear()
        await cb.message.answer("Выбери действие.", reply_markup=menu(service))

    @router.callback_query(F.data == "buy")
    async def buy(cb, state: FSMContext):
        await cb.answer()
        await state.clear()
        events = service.events()
        if not events:
            await cb.message.answer("Сейчас нет мероприятий в продаже.", reply_markup=menu(service))
        elif len(events) == 1:
            await show_event(cb.message, events[0]["id"])
        else:
            await cb.message.answer("Выбери мероприятие.", reply_markup=keyboard(
                [[(e["title"], "event:" + e["id"])] for e in events] + [[("Назад", "home")]]))

    @router.callback_query(F.data.startswith("event:"))
    async def choose_event(cb, state: FSMContext):
        await cb.answer()
        await state.clear()
        await show_event(cb.message, cb.data.split(":", 1)[1])

    @router.callback_query(F.data.startswith("tariff:"))
    async def tariff(cb, state: FSMContext):
        await cb.answer()
        _, event_id, tariff_id = cb.data.split(":")
        event = service.event(event_id)
        if not event:
            raise BusinessError("Продажи завершены.")
        item = next((t for t in event["tariffs"] if t["id"] == tariff_id), None)
        if not item:
            raise BusinessError("Тариф недоступен.")
        await state.clear()
        await state.update_data(event_id=event_id, tariff_id=tariff_id)
        text = f"<b>{esc(item['name'])} — {rub(int(item['price']*100))}</b>\n\n{esc(item['description'])}"
        def document_link(label, url):
            return f'<a href="{esc(url, quote=True)}">{label}</a>' if url else label

        text += ("\n\nНажимая «Оформить», ты соглашаешься с "
            + document_link("Правилами мероприятия", service.settings.rules_url) + ", "
            + document_link("Политикой конфиденциальности", service.settings.privacy_url) + " и "
            + document_link("условиями Оферты", service.settings.offer_url) + ".")
        rows = [[("Оформить", "checkout")]]
        for title, url in (("Правила",service.settings.rules_url),("Политика",service.settings.privacy_url),("Оферта",service.settings.offer_url)):
            if url:
                rows.append([(title,url)])
        rows.append([("Назад", "event:" + event_id)])
        await cb.message.answer(text,parse_mode="HTML",reply_markup=keyboard(rows))

    @router.callback_query(F.data == "checkout")
    async def checkout(cb, state: FSMContext):
        await cb.answer()
        if not service.settings.payments_enabled:
            raise BusinessError("Оплата скоро появится. Пока можно посмотреть тарифы или написать менеджеру.")
        if not (await state.get_data()).get("tariff_id"):
            raise BusinessError("Выбери тариф заново.")
        await state.set_state(Checkout.email)
        await cb.message.answer("Укажи email для отправки чека.", reply_markup=keyboard([[('Назад', 'buy')]]))

    @router.message(Checkout.email, F.text)
    async def email(message, state: FSMContext):
        try:
            email_value = validate_email(message.text.strip(),check_deliverability=False).normalized
        except EmailNotValidError:
            await message.answer("Проверь email. Например: name@example.com")
            return
        await state.update_data(email=email_value)
        await state.set_state(Checkout.name)
        await message.answer("Укажи ФИО покупателя.", reply_markup=keyboard([[('Назад', 'back_email')]]))

    @router.callback_query(F.data == "back_email")
    async def back_email(cb, state: FSMContext):
        await cb.answer()
        await state.set_state(Checkout.email)
        await cb.message.answer("Укажи email для отправки чека.")

    @router.message(Checkout.name, F.text)
    async def name(message, state: FSMContext):
        value = " ".join(message.text.split())
        if len(value) < 2 or len(value) > 200 or not any(c.isalpha() for c in value):
            await message.answer("Укажи имя покупателя: от 2 до 200 символов.")
            return
        await state.update_data(full_name=value)
        await state.set_state(Checkout.phone)
        await message.answer("Укажи телефон покупателя. Например: +79991234567", reply_markup=keyboard([[('Назад', 'back_name')]]))

    @router.callback_query(F.data == "back_name")
    async def back_name(cb, state: FSMContext):
        await cb.answer()
        await state.set_state(Checkout.name)
        await cb.message.answer("Укажи ФИО покупателя.")

    @router.message(Checkout.phone, F.text)
    async def phone(message, state: FSMContext):
        value = re.sub(r"[\s()\-]", "", message.text)
        if re.fullmatch(r"8\d{10}", value):
            value = "+7" + value[1:]
        if re.fullmatch(r"7\d{10}", value):
            value = "+" + value
        if not re.fullmatch(r"\+[1-9]\d{7,14}", value):
            await message.answer("Проверь телефон. Например: +79991234567")
            return
        data = await state.get_data()
        if not all(data.get(k) for k in ("event_id","tariff_id","email","full_name")):
            await state.clear()
            raise BusinessError("Начни оформление заново через «Купить билет».")
        order_id = uuid.uuid4().hex
        service.create_order(order_id,message.from_user.id,data["event_id"],data["tariff_id"],data["email"],data["full_name"],value)
        order = service.order(order_id)
        snapshot = json.loads(order["snapshot"])
        await state.update_data(phone=value,order_id=order_id)
        await state.set_state(Checkout.review)
        text = ("<b>Проверь данные:</b>\n\n" + esc(snapshot['event']['title']) + "\n"
            + f"Тариф: {esc(snapshot['tariff']['name'])}\nСумма: {rub(order['amount'])}\n"
            + f"Email: {esc(order['email'])}\nФИО: {esc(order['full_name'])}\nТелефон: {esc(value)}\n\n"
            + "После оплаты билеты появятся в «Моих билетах».")
        await message.answer(text,parse_mode="HTML",reply_markup=keyboard([
            [("К оплате", "pay:" + order_id)],[("Изменить данные", "checkout")],[("Назад", "buy")]]))

    @router.callback_query(F.data.startswith("pay:"))
    async def pay(cb):
        await cb.answer()
        url = await service.pay(cb.data.split(":",1)[1],cb.from_user.id)
        await cb.message.answer("Нажми кнопку ниже, чтобы перейти к оплате.\nПосле успешной оплаты билеты появятся в «Моих билетах».",
            reply_markup=keyboard([[("Оплатить в ЮKassa",url)],[("Проверить оплату","check:" + cb.data.split(":",1)[1])],[("Главное меню","home")]]))

    @router.callback_query(F.data.startswith("check:"))
    async def check(cb):
        await cb.answer()
        order = service.order(cb.data.split(":",1)[1],cb.from_user.id)
        if order["status"] == "paid":
            await show_tickets(cb.message,cb.from_user.id)
        else:
            texts = {"pending":"Проверяем оплату. Билеты появятся в «Моих билетах».",
                "canceled":"Оплата не завершена. Попробуй ещё раз, если билет всё ещё нужен.",
                "error":"Не удалось создать оплату. Напиши менеджеру.",
                "refund_pending":"Обрабатываем возврат.","refunded":"Возврат оформлен. Билеты недействительны.",
                "late_paid":"Оплата поступила после закрытия продаж. Оформляем полный возврат.",
                "late_refund_error":"Оплата поступила после закрытия продаж. Для возврата напиши менеджеру."}
            await cb.message.answer(texts.get(order["status"],"Открой «Мои билеты»."),reply_markup=menu(service))

    @router.callback_query(F.data == "tickets")
    async def tickets(cb, state: FSMContext):
        await cb.answer()
        await state.clear()
        await show_tickets(cb.message,cb.from_user.id)

    @router.callback_query(F.data.startswith("refund:"))
    async def refund_confirm(cb):
        await cb.answer()
        order = service.order(cb.data.split(":",1)[1],cb.from_user.id)
        with service.db.connect() as con:
            service.check_refund(order,con)
        label = f"Вернуть заказ — {order['quantity']} билетов, {rub(order['amount'])}" if order["quantity"] > 1 else f"Вернуть билет — {rub(order['amount'])}"
        await cb.message.answer(label + "?\n\nПосле возврата все билеты этого заказа станут недействительными.",
            reply_markup=keyboard([[(label,"refund_yes:" + order["id"])],[("Не возвращать","tickets")]]))

    @router.callback_query(F.data.startswith("refund_yes:"))
    async def refund(cb):
        await cb.answer()
        await cb.message.answer("Обрабатываем возврат.")
        status = await service.refund(cb.data.split(":",1)[1],cb.from_user.id)
        text = {"succeeded":"Возврат оформлен. Билеты больше недействительны.",
            "canceled":"Возврат не выполнен. Напиши менеджеру.","error":"Возврат не выполнен. Напиши менеджеру."}.get(status,
            "Возврат обрабатывается. Сообщим о результате.")
        await cb.message.answer(text,reply_markup=menu(service))

    @router.message()
    async def fallback(message):
        await message.answer("Для оформления используй кнопки. Если диалог прервался, начни с /start.",reply_markup=menu(service))

    @router.callback_query()
    async def stale(cb):
        await cb.answer("Кнопка устарела. Открой /start.",show_alert=True)

    @dp.errors()
    async def error(event: ErrorEvent):
        exc = event.exception
        if isinstance(exc,BusinessError):
            text = str(exc)
        elif isinstance(exc,ProviderError):
            text = "Не удалось получить ответ от ЮKassa. Попробуй проверить оплату чуть позже. Если проблема остаётся — напиши менеджеру."
        else:
            text = "Не получилось выполнить действие. Попробуй ещё раз или напиши менеджеру."
        log.error("bot_error type=%s",type(exc).__name__)
        message = event.update.message or (event.update.callback_query.message if event.update.callback_query else None)
        if message:
            await message.answer(text,reply_markup=menu(service))
        return True

    return dp


async def notifications(bot, service):
    for order in service.db.all("SELECT * FROM orders WHERE notified=0 AND status IN ('paid','canceled','refunded','late_paid','late_refund_error','refund_pending') LIMIT 50"):
        try:
            status = order["status"]
            refund = service.db.one("SELECT status,reason FROM refunds WHERE order_id=?",(order["id"],))
            if status == "paid":
                if refund and refund["status"] in ("canceled","error"):
                    await bot.send_message(order["user_id"],"Возврат не выполнен. Билеты действуют. Напиши менеджеру.",reply_markup=menu(service))
                else:
                    items = [t for t in service.tickets(order["user_id"]) if t["order_id"] == order["id"]]
                    for ticket in items:
                        await send_ticket(bot,service,order["user_id"],ticket)
            else:
                text = {"canceled":"Оплата не завершена. Попробуй ещё раз, если билет всё ещё нужен.",
                    "refunded":"Возврат оформлен. Билеты больше недействительны.",
                    "late_paid":"Оплата поступила после закрытия продаж. Оформляем полный возврат.",
                    "late_refund_error":"Оплата поступила после закрытия продаж. Для возврата напиши менеджеру.",
                    "refund_pending":"Обрабатываем возврат. Сообщим о результате."}[status]
                if refund and refund["reason"] == "late" and status in ("refund_pending","refunded"):
                    text = "Оплата поступила после закрытия продаж. " + text
                await bot.send_message(order["user_id"],text,reply_markup=menu(service))
            with service.db.transaction() as con:
                con.execute("UPDATE orders SET notified=1 WHERE id=? AND status=?",(order["id"],status))
        except Exception as exc:
            log.warning("notification_deferred order=%s type=%s",order["id"],type(exc).__name__)


async def worker(bot, service):
    while True:
        try:
            if service.settings.payments_enabled:
                await service.maintain()
            await notifications(bot,service)
        except Exception as exc:
            log.error("worker_error type=%s",type(exc).__name__)
        await asyncio.sleep(15)
