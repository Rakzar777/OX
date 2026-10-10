import copy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.service import Service


class FakeProvider:
    def __init__(self):
        self.payments, self.refunds = {}, {}
        self.payment_keys, self.refund_keys = [], []
        self.next_refund_status = "succeeded"
        self.captured_at = "2026-10-20T12:00:00+03:00"
        self.payment_error = None
        self.refund_error = None

    async def create_payment(self, payload, key):
        self.payment_keys.append(key)
        if self.payment_error:
            raise self.payment_error
        id = "payment-" + key
        self.payments.setdefault(id, {"id":id,"amount":payload["amount"],"metadata":payload["metadata"],
            "status":"pending","paid":False,"test":True,
            "confirmation":{"confirmation_url":"https://yookassa.ru/test/" + id}})
        return copy.deepcopy(self.payments[id])

    async def get_payment(self, id):
        return copy.deepcopy(self.payments[id])

    async def create_refund(self, payload, key):
        self.refund_keys.append(key)
        if self.refund_error:
            raise self.refund_error
        id = "refund-" + key
        self.refunds.setdefault(id,{"id":id,"payment_id":payload["payment_id"],"amount":payload["amount"],"status":self.next_refund_status})
        return copy.deepcopy(self.refunds[id])

    async def get_refund(self,id):
        return copy.deepcopy(self.refunds[id])


@pytest.fixture
def service(tmp_path):
    settings = Settings(database_path=str(tmp_path / "test.sqlite3"),bot_username="test_bot")
    db = Database(settings.database_path)
    db.migrate()
    db.seed(Path(__file__).parent / "events.json")
    return Service(db,settings,FakeProvider(),now=lambda:datetime(2026,10,20,9,tzinfo=timezone.utc))


async def paid(service, tariff="company", order_id="a" * 32, user=101):
    service.create_order(order_id,user,"halloween-2026",tariff,"buyer@example.com","Иван Иванов","+79991234567")
    await service.pay(order_id,user)
    p = service.db.one("SELECT * FROM payments WHERE order_id=?",(order_id,))
    obj = service.provider.payments[p["id"]]
    obj.update(status="succeeded",paid=True,captured_at=service.provider.captured_at)
    await service.payment_webhook(p["id"])
    return order_id,p["id"]
