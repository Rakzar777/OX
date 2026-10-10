import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from app.provider import ProviderError
from app.service import BusinessError
from conftest import paid


@pytest.mark.asyncio
async def test_disabled_payments_do_not_create_orders_or_call_provider(service):
    service.settings.payments_enabled=False
    assert service.events()
    with pytest.raises(BusinessError,match="Оплата скоро"):
        service.create_order("disabled",101,"halloween-2026","standard","a@example.com","Имя","+79991234567")
    with pytest.raises(BusinessError,match="Оплата скоро"):
        await service.pay("disabled",101)
    assert not service.db.all("SELECT * FROM orders")
    assert not service.provider.payment_keys


@pytest.mark.asyncio
async def test_six_tickets_duplicate_webhooks_and_restart(service):
    order,pid = await paid(service)
    await asyncio.gather(*(service.payment_webhook(pid) for _ in range(10)))
    tickets = service.tickets(101)
    assert len(tickets) == 6
    assert len({t['token'] for t in tickets}) == 6
    from app.service import Service
    restarted = Service(service.db,service.settings,service.provider,service.now)
    await restarted.payment_webhook(pid)
    assert [t['token'] for t in restarted.tickets(101)] == [t['token'] for t in tickets]


@pytest.mark.asyncio
async def test_no_tickets_before_webhook_and_single_payment(service):
    oid = "b"*32
    service.create_order(oid,101,"halloween-2026","standard","a@example.com","Имя","+79991234567")
    urls = await asyncio.gather(*(service.pay(oid,101) for _ in range(10)))
    assert len(set(urls)) == 1
    assert len(service.provider.payment_keys) == 1
    assert service.tickets(101) == []


@pytest.mark.asyncio
async def test_concurrent_redemption_one_winner(service):
    await paid(service)
    token = service.tickets(101)[0]["token"]
    def redeem():
        try:
            service.redeem(token)
            return True
        except BusinessError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(lambda _:redeem(),range(8))) == 1
    assert service.ticket(token)["status"] == "used"


@pytest.mark.asyncio
@pytest.mark.parametrize("at,allowed",[("2026-10-22T17:59:59+03:00",True),
    ("2026-10-22T18:00:00+03:00",True),("2026-10-22T18:00:00.000001+03:00",False)])
async def test_refund_boundary(service,at,allowed):
    oid,_ = await paid(service)
    service.now = lambda:datetime.fromisoformat(at)
    if allowed:
        assert await service.refund(oid,101) == "succeeded"
        assert service.tickets(101) == []
        assert len(service.db.all("SELECT * FROM tickets WHERE status='refunded'")) == 6
    else:
        with pytest.raises(BusinessError):
            await service.refund(oid,101)


@pytest.mark.asyncio
async def test_used_group_cannot_refund(service):
    oid,_ = await paid(service)
    service.redeem(service.tickets(101)[0]["token"])
    with pytest.raises(BusinessError):
        await service.refund(oid,101)
    assert not service.provider.refund_keys


@pytest.mark.asyncio
async def test_pending_refund_blocks_use_and_is_idempotent(service):
    oid,_ = await paid(service)
    token = service.tickets(101)[0]["token"]
    service.provider.next_refund_status = "pending"
    assert await service.refund(oid,101) == "pending"
    with pytest.raises(BusinessError):
        service.redeem(token)
    await service.refund(oid,101)
    assert len(service.provider.refund_keys) == 1
    obj = next(iter(service.provider.refunds.values()))
    obj["status"] = "succeeded"
    await service.refund_webhook(obj["id"])
    await service.refund_webhook(obj["id"])
    assert not service.tickets(101)
    with pytest.raises(BusinessError):
        service.redeem(token)


@pytest.mark.asyncio
async def test_refund_rejected_restores_ticket(service):
    oid,_ = await paid(service,"standard")
    service.provider.next_refund_status = "canceled"
    assert await service.refund(oid,101) == "canceled"
    service.redeem(service.tickets(101)[0]["token"])


@pytest.mark.asyncio
async def test_refund_network_timeout_reuses_key(service):
    oid,_ = await paid(service)
    service.provider.refund_error = ProviderError()
    with pytest.raises(ProviderError):
        await service.refund(oid,101)
    assert service.order(oid)["status"] == "refund_pending"
    service.provider.refund_error = None
    await service.maintain()
    assert len(set(service.provider.refund_keys)) == 1
    assert service.order(oid)["status"] == "refunded"


@pytest.mark.asyncio
async def test_order_owner_and_amount_validation(service):
    oid,pid = await paid(service)
    with pytest.raises(BusinessError):
        await service.refund(oid,999)
    service.provider.payments[pid]["amount"]["value"] = "1.00"
    with pytest.raises(ValueError):
        await service.payment_webhook(pid)


@pytest.mark.asyncio
async def test_sales_cutoff_also_blocks_old_buttons(service):
    oid = "c"*32
    service.create_order(oid,101,"halloween-2026","standard","a@example.com","Имя","+79991234567")
    service.now = lambda:datetime.fromisoformat("2026-10-26T12:00:00+03:00")
    assert not service.events()
    with pytest.raises(BusinessError):
        await service.pay(oid,101)


@pytest.mark.asyncio
async def test_late_capture_refunded_without_tickets(service):
    service.provider.captured_at = "2026-10-26T12:00:00+03:00"
    oid,_ = await paid(service)
    assert service.order(oid)["status"] == "late_paid"
    assert not service.tickets(101)
    await service.maintain()
    assert service.order(oid)["status"] == "refunded"


@pytest.mark.asyncio
async def test_delayed_webhook_uses_capture_time(service):
    oid,pid = await paid(service,"standard")
    service.now = lambda:datetime.fromisoformat("2026-10-27T12:00:00+03:00")
    await service.payment_webhook(pid)
    assert len(service.tickets(101)) == 1


@pytest.mark.asyncio
async def test_payment_timeout_reuses_persisted_payload_and_key(service):
    oid = "d"*32
    service.create_order(oid,101,"halloween-2026","standard","a@example.com","Имя","+79991234567")
    service.provider.payment_error = ProviderError()
    with pytest.raises(ProviderError):
        await service.pay(oid,101)
    payload = service.db.one("SELECT payload FROM payments WHERE order_id=?",(oid,))["payload"]
    service.provider.payment_error = None
    service.settings.vat_code = 2
    await service.pay(oid,101)
    assert len(set(service.provider.payment_keys)) == 1
    assert service.db.one("SELECT payload FROM payments WHERE order_id=?",(oid,))["payload"] == payload


@pytest.mark.asyncio
async def test_expired_idempotency_key_not_replayed(service):
    oid = "e"*32
    service.create_order(oid,101,"halloween-2026","standard","a@example.com","Имя","+79991234567")
    service.provider.payment_error = ProviderError()
    with pytest.raises(ProviderError):
        await service.pay(oid,101)
    service.now = lambda:datetime.fromisoformat("2026-10-22T12:00:00+03:00")
    with pytest.raises(BusinessError):
        await service.pay(oid,101)
    assert len(service.provider.payment_keys) == 1


@pytest.mark.asyncio
async def test_hidden_event_preserves_tickets(service,tmp_path):
    await paid(service)
    events = tmp_path / "empty.json"
    events.write_text("[]")
    service.db.seed(events)
    assert not service.events()
    assert len(service.tickets(101)) == 6

@pytest.mark.asyncio
async def test_removed_tariff_cannot_start_payment(service):
    import json
    from app.service import BusinessError
    service.create_order('removed',101,'halloween-2026','company','test@example.com','Test Buyer','+79991234567')
    event = service.event('halloween-2026')
    event['tariffs'] = [t for t in event['tariffs'] if t['id'] != 'company']
    with service.db.transaction() as con:
        con.execute('UPDATE events SET data=? WHERE id=?',(json.dumps(event),'halloween-2026'))
    with pytest.raises(BusinessError, match='больше не продаётся'):
        await service.pay('removed',101)
    assert not service.provider.payment_keys
