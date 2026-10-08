import asyncio
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from .provider import ProviderError

log = logging.getLogger(__name__)
MSK = ZoneInfo("Europe/Moscow")


def utcnow():
    return datetime.now(timezone.utc)


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def money(kopecks):
    return f"{kopecks / 100:.2f}"


def rub(kopecks):
    return money(kopecks).removesuffix(".00") + " ₽"


class BusinessError(Exception):
    pass


class Service:
    def __init__(self, db, settings, provider, now=utcnow):
        self.db, self.settings, self.provider, self.now = db, settings, provider, now
        # One process by design. SQLite transactions also protect redemption across threads.
        self.payment_lock = asyncio.Lock()
        self.refund_lock = asyncio.Lock()

    def events(self):
        result = [json.loads(r["data"]) for r in self.db.all("SELECT data FROM events WHERE enabled=1")]
        return sorted([e for e in result if self.now() < dt(e["sales_close_at"])], key=lambda e: e["starts_at"])

    def event(self, event_id):
        return next((e for e in self.events() if e["id"] == event_id), None)

    def order(self, order_id, user_id=None):
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order or (user_id is not None and order["user_id"] != user_id):
            raise BusinessError("Заказ не найден.")
        return order

    def create_order(self, order_id, user_id, event_id, tariff_id, email, full_name, phone):
        if not self.settings.payments_enabled:
            raise BusinessError("Оплата скоро появится. Пока можно посмотреть тарифы или написать менеджеру.")
        event = self.event(event_id)
        if not event:
            raise BusinessError("Продажи завершены.")
        tariff = next((t for t in event["tariffs"] if t["id"] == tariff_id), None)
        if not tariff:
            raise BusinessError("Тариф недоступен. Выбери другой.")
        amount = int(Decimal(str(tariff["price"])) * 100)
        with self.db.transaction() as con:
            old = con.execute("SELECT user_id FROM orders WHERE id=?", (order_id,)).fetchone()
            if old:
                if old["user_id"] != user_id:
                    raise BusinessError("Заказ не найден.")
                return order_id
            con.execute("INSERT OR IGNORE INTO users VALUES(?)", (user_id,))
            con.execute("""INSERT INTO orders(id,user_id,event_id,snapshot,email,full_name,phone,amount,quantity,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (order_id,user_id,event_id,
                json.dumps({"event": event, "tariff": tariff}, ensure_ascii=False),
                email,full_name,phone,amount,tariff["quantity"],self.now().timestamp()))
            con.execute("INSERT INTO order_items VALUES(?,?,?,?,?)",
                (order_id,tariff_id,tariff["name"],tariff["quantity"],amount))
        log.info("order_created order=%s", order_id)
        return order_id

    def receipt(self, order):
        snap = json.loads(order["snapshot"])
        result = {"customer": {"email": order["email"], "phone": order["phone"], "full_name": order["full_name"]},
            "items": [{"description": (snap["event"]["title"] + ": " + snap["tariff"]["name"])[:128],
                "quantity": "1.000", "amount": {"value": money(order["amount"]), "currency": "RUB"},
                "vat_code": self.settings.vat_code, "payment_mode": self.settings.payment_mode,
                "payment_subject": self.settings.payment_subject}]}
        if self.settings.tax_system_code:
            result["tax_system_code"] = self.settings.tax_system_code
        return result

    def check_amount(self, obj, order):
        if obj.get("amount", {}).get("currency") != "RUB":
            raise ValueError("Валюта не совпадает")
        if Decimal(obj["amount"]["value"]) * 100 != order["amount"]:
            raise ValueError("Сумма не совпадает")

    async def pay(self, order_id, user_id):
        if not self.settings.payments_enabled:
            raise BusinessError("Оплата скоро появится. Пока можно посмотреть тарифы или написать менеджеру.")
        async with self.payment_lock:
            order = self.order(order_id, user_id)
            event = json.loads(order["snapshot"])["event"]
            if self.now() >= dt(event["sales_close_at"]) or not self.event(order["event_id"]):
                raise BusinessError("Продажи завершены.")
            if order["status"] not in ("new", "pending"):
                raise BusinessError("Этот заказ уже обработан. Открой «Мои билеты» или начни новую покупку.")
            payment = self.db.one("SELECT * FROM payments WHERE order_id=?", (order_id,))
            if payment and payment["url"] and payment["status"] == "pending":
                return payment["url"]
            if not payment:
                payload = {"amount": {"value": money(order["amount"]), "currency": "RUB"}, "capture": True,
                    "confirmation": {"type": "redirect", "return_url": self.settings.public_base_url + "/payment-return"},
                    "description": "Билеты, заказ " + order_id,
                    "metadata": {"order_id": order_id}, "receipt": self.receipt(order)}
                with self.db.transaction() as con:
                    con.execute("INSERT INTO payments(order_id,idem_key,payload,created_at) VALUES(?,?,?,?)",
                                (order_id, str(uuid.uuid4()), json.dumps(payload), self.now().timestamp()))
                    con.execute("UPDATE orders SET status='pending' WHERE id=?", (order_id,))
                payment = self.db.one("SELECT * FROM payments WHERE order_id=?", (order_id,))
            return await self._create_payment(payment)

    async def _create_payment(self, payment):
        if payment["status"] == "error":
            raise BusinessError("Не удалось создать оплату. Напиши менеджеру или начни новую покупку.")
        if self.now().timestamp() - payment["created_at"] >= 23 * 3600:
            raise BusinessError("Статус платежа требует проверки менеджером. Не оплачивай повторно.")
        try:
            obj = await self.provider.create_payment(json.loads(payment["payload"]), payment["idem_key"])
        except ProviderError as exc:
            if exc.definite:
                with self.db.transaction() as con:
                    con.execute("UPDATE payments SET status='error' WHERE order_id=? AND id IS NULL", (payment["order_id"],))
                    con.execute("UPDATE orders SET status='error' WHERE id=? AND status='pending'", (payment["order_id"],))
            raise
        order = self.order(payment["order_id"])
        self.check_amount(obj, order)
        if obj.get("metadata", {}).get("order_id") != order["id"] or obj.get("test") != self.settings.test:
            raise ValueError("Ответ платежной системы не соответствует заказу или режиму")
        url = obj.get("confirmation", {}).get("confirmation_url")
        with self.db.transaction() as con:
            current = con.execute("SELECT id FROM payments WHERE order_id=?", (order["id"],)).fetchone()
            if current["id"] and current["id"] != obj["id"]:
                raise ValueError("Другой платеж уже привязан")
            con.execute("""UPDATE payments SET id=?,url=COALESCE(?,url),
                status=CASE WHEN status='creating' THEN 'pending' ELSE status END WHERE order_id=?""",
                (obj["id"],url,order["id"]))
        if not url:
            raise BusinessError("Проверяем оплату. Билеты появятся в «Моих билетах».")
        log.info("payment_created order=%s", order["id"])
        return url

    async def payment_webhook(self, payment_id):
        # Trust only authenticated provider API, never the callback payload.
        obj = await self.provider.get_payment(payment_id)
        if obj.get("id") != payment_id or obj.get("test") != self.settings.test:
            raise ValueError("Платеж не соответствует режиму магазина")
        order_id = obj.get("metadata", {}).get("order_id")
        payment = self.db.one("SELECT * FROM payments WHERE order_id=?", (order_id,))
        if not payment:
            return  # Another application may share the same shop.
        order = self.order(order_id)
        self.check_amount(obj, order)
        if payment["id"] and payment["id"] != payment_id:
            raise ValueError("Платеж не совпадает")
        state = obj.get("status")
        if state not in ("succeeded", "canceled"):
            return
        late = False
        if state == "succeeded":
            if obj.get("paid") is not True or not obj.get("captured_at"):
                raise ValueError("Нет подтверждения списания")
            cutoff = dt(json.loads(order["snapshot"])["event"]["sales_close_at"])
            late = dt(obj["captured_at"]) >= cutoff
        with self.db.transaction() as con:
            current = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            bound = con.execute("SELECT id FROM payments WHERE order_id=?", (order_id,)).fetchone()
            if bound["id"] and bound["id"] != payment_id:
                raise ValueError("Платеж не совпадает")
            if current["status"] not in ("new", "pending", "error"):
                return
            con.execute("UPDATE payments SET id=?,status=? WHERE order_id=?", (payment_id,state,order_id))
            if state == "canceled":
                con.execute("UPDATE orders SET status='canceled',notified=0 WHERE id=?", (order_id,))
            elif late:
                con.execute("UPDATE orders SET status='late_paid',notified=0 WHERE id=?", (order_id,))
            else:
                con.execute("UPDATE orders SET status='paid',notified=0 WHERE id=?", (order_id,))
                for n in range(order["quantity"]):
                    con.execute("INSERT OR IGNORE INTO tickets(id,order_id,position,token) VALUES(?,?,?,?)",
                                (uuid.uuid4().hex,order_id,n+1,secrets.token_urlsafe(32)))
        log.info("payment_%s order=%s late=%s", state, order_id, late)

    def tickets(self, user_id):
        return self.db.all("""SELECT t.*,o.snapshot,o.full_name,o.status AS order_status FROM tickets t
            JOIN orders o ON o.id=t.order_id WHERE o.user_id=? AND t.status='active'
            AND o.status IN ('paid','refund_pending') ORDER BY o.created_at DESC,t.position""", (user_id,))

    def ticket(self, token):
        return self.db.one("""SELECT t.*,o.snapshot,o.full_name,o.status AS order_status FROM tickets t
            JOIN orders o ON o.id=t.order_id WHERE t.token=?""", (token,))

    def redeem(self, token):
        with self.db.transaction() as con:
            row = con.execute("""SELECT t.id,t.status,o.status AS order_status FROM tickets t
                JOIN orders o ON o.id=t.order_id WHERE t.token=?""", (token,)).fetchone()
            if not row:
                raise BusinessError("Билет не найден.")
            if row["status"] != "active":
                raise BusinessError("Билет уже использован или недействителен.")
            if row["order_status"] != "paid":
                raise BusinessError("Билет недоступен: обрабатывается возврат.")
            con.execute("UPDATE tickets SET status='used',used_at=? WHERE id=? AND status='active'",
                        (self.now().timestamp(),row["id"]))
        log.info("ticket_used id=%s", row["id"])

    def check_refund(self, order, con):
        if order["status"] != "paid":
            raise BusinessError("Возврат недоступен или уже обрабатывается.")
        start = dt(json.loads(order["snapshot"])["event"]["starts_at"])
        if self.now() > start - timedelta(hours=72):
            raise BusinessError("Возврат недоступен: до начала меньше 72 часов.")
        rows = con.execute("SELECT status FROM tickets WHERE order_id=?", (order["id"],)).fetchall()
        if len(rows) != order["quantity"] or any(r["status"] != "active" for r in rows):
            raise BusinessError("Возврат недоступен: один из билетов уже использован или недействителен.")

    async def refund(self, order_id, user_id=None, late=False):
        async with self.refund_lock:
            order = self.order(order_id, user_id)
            with self.db.transaction() as con:
                order = dict(con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())
                existing = con.execute("SELECT * FROM refunds WHERE order_id=?", (order_id,)).fetchone()
                if existing:
                    if existing["status"] in ("succeeded", "canceled", "error"):
                        return existing["status"]
                else:
                    if late:
                        if order["status"] != "late_paid":
                            raise BusinessError("Заказ не ожидает возврата.")
                    else:
                        self.check_refund(order, con)
                    payment = con.execute("SELECT id FROM payments WHERE order_id=?", (order_id,)).fetchone()
                    payload = {"payment_id": payment["id"],
                        "amount": {"value": money(order["amount"]), "currency": "RUB"},
                        "description": "Возврат заказа " + order_id, "receipt": self.receipt(order)}
                    con.execute("INSERT INTO refunds(order_id,idem_key,payload,reason,created_at) VALUES(?,?,?,?,?)",
                        (order_id,str(uuid.uuid4()),json.dumps(payload),"late" if late else "buyer",self.now().timestamp()))
                    con.execute("UPDATE orders SET status='refund_pending',notified=0 WHERE id=?", (order_id,))
            return await self._submit_refund(self.db.one("SELECT * FROM refunds WHERE order_id=?", (order_id,)))

    async def _submit_refund(self, refund):
        if refund["id"]:
            obj = await self.provider.get_refund(refund["id"])
        else:
            if self.now().timestamp() - refund["created_at"] >= 23 * 3600:
                raise BusinessError("Возврат требует проверки менеджером.")
            try:
                obj = await self.provider.create_refund(json.loads(refund["payload"]),refund["idem_key"])
            except ProviderError as exc:
                if exc.definite:
                    with self.db.transaction() as con:
                        con.execute("UPDATE refunds SET status='error' WHERE order_id=? AND id IS NULL", (refund["order_id"],))
                        status = "late_refund_error" if refund["reason"] == "late" else "paid"
                        con.execute("UPDATE orders SET status=?,notified=0 WHERE id=? AND status='refund_pending'", (status,refund["order_id"]))
                raise
        await self.apply_refund(obj)
        return obj["status"]

    async def refund_webhook(self, refund_id):
        obj = await self.provider.get_refund(refund_id)
        if obj.get("id") != refund_id:
            raise ValueError("Возврат не совпадает")
        await self.apply_refund(obj)

    async def apply_refund(self, obj):
        payment = self.db.one("SELECT order_id FROM payments WHERE id=?", (obj.get("payment_id"),))
        if not payment:
            return
        order = self.order(payment["order_id"])
        self.check_amount(obj, order)
        status = obj.get("status")
        if status not in ("pending", "succeeded", "canceled"):
            raise ValueError("Неизвестный статус возврата")
        with self.db.transaction() as con:
            refund = con.execute("SELECT * FROM refunds WHERE order_id=?", (order["id"],)).fetchone()
            if refund and refund["id"] and refund["id"] != obj["id"]:
                raise ValueError("Другой возврат уже привязан")
            if refund and refund["status"] == "succeeded":
                return
            if not refund:
                # A full refund made by the organizer in YooKassa also invalidates tickets.
                con.execute("INSERT INTO refunds(order_id,id,idem_key,payload,status,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                    (order["id"],obj["id"],str(uuid.uuid4()),"{}",status,"external",self.now().timestamp()))
            else:
                con.execute("UPDATE refunds SET id=?,status=? WHERE order_id=?", (obj["id"],status,order["id"]))
            if status == "succeeded":
                con.execute("UPDATE tickets SET status='refunded' WHERE order_id=?", (order["id"],))
                con.execute("UPDATE orders SET status='refunded',notified=0 WHERE id=?", (order["id"],))
            elif status == "canceled":
                next_status = "late_refund_error" if refund and refund["reason"] == "late" else "paid"
                con.execute("UPDATE orders SET status=?,notified=0 WHERE id=?", (next_status,order["id"]))
            else:
                con.execute("UPDATE orders SET status='refund_pending' WHERE id=? AND status!='refunded'", (order["id"],))
        log.info("refund_%s order=%s",status,order["id"])

    async def maintain(self):
        # Only retry requests whose result is uncertain, and monitor initiated refunds.
        # Payment completion is processed exclusively through a verified webhook.
        for p in self.db.all("SELECT * FROM payments WHERE status='creating'"):
            try:
                async with self.payment_lock:
                    fresh = self.db.one("SELECT * FROM payments WHERE order_id=?", (p["order_id"],))
                    if fresh["status"] == "creating":
                        await self._create_payment(fresh)
            except (ProviderError, BusinessError, ValueError):
                log.warning("payment_retry_deferred order=%s", p["order_id"])
        for o in self.db.all("SELECT id FROM orders WHERE status='late_paid'"):
            try:
                await self.refund(o["id"], late=True)
            except (ProviderError, BusinessError, ValueError):
                log.warning("late_refund_deferred order=%s", o["id"])
        for r in self.db.all("SELECT * FROM refunds WHERE status IN ('creating','pending')"):
            try:
                async with self.refund_lock:
                    fresh = self.db.one("SELECT * FROM refunds WHERE order_id=?", (r["order_id"],))
                    if fresh["status"] in ("creating", "pending"):
                        await self._submit_refund(fresh)
            except (ProviderError, BusinessError, ValueError):
                log.warning("refund_retry_deferred order=%s", r["order_id"])
