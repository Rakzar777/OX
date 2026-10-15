from dataclasses import replace
from pathlib import Path

import pytest

from app.db import Database
from app.service import Service
from conftest import FakeProvider, paid
from scripts.merge_databases import merge_databases


def destination(service, tmp_path):
    settings = replace(service.settings, database_path=str(tmp_path / "new.sqlite3"))
    db = Database(settings.database_path)
    db.migrate()
    db.seed(Path(__file__).parent / "events.json")
    return Service(db, settings, FakeProvider(), service.now)


@pytest.mark.asyncio
async def test_merge_preserves_current_orders_tokens_redemption_and_refunds(service, tmp_path):
    old_order, _ = await paid(service)
    service.redeem(service.tickets(101)[0]["token"])
    refunded, _ = await paid(service, "vip", "b" * 32)
    await service.refund(refunded, 101)
    current = destination(service, tmp_path)
    await paid(current, "standard", "c" * 32, user=202)
    before = current.db.all("SELECT * FROM tickets")
    old_tickets = service.db.all("SELECT * FROM tickets ORDER BY id")
    dry = merge_databases(service.db.path, current.db.path)
    assert dry["inserted"]["orders"] == 2
    assert current.db.all("SELECT * FROM tickets") == before
    merge_databases(service.db.path, current.db.path, apply=True)
    assert current.db.all("SELECT * FROM tickets WHERE order_id=?", ("c" * 32,)) == before
    assert current.db.all("SELECT * FROM tickets WHERE order_id IN (?,?) ORDER BY id", (old_order, refunded)) == old_tickets
    assert current.order(refunded)["status"] == "refunded"
    assert len(current.tickets(101)) == 5
    assert merge_databases(service.db.path, current.db.path, apply=True)["inserted"]["tickets"] == 0


@pytest.mark.asyncio
async def test_merge_conflict_rolls_back_everything(service, tmp_path):
    await paid(service, "standard", "a" * 32)
    current = destination(service, tmp_path)
    await paid(current, "vip", "a" * 32, user=202)
    before = current.db.all("SELECT * FROM orders")
    with pytest.raises(ValueError, match="Conflicting"):
        merge_databases(service.db.path, current.db.path, apply=True)
    assert current.db.all("SELECT * FROM orders") == before
    assert not current.db.one("SELECT * FROM users WHERE id=101")
