"""Local ticket-page demo. No Telegram or YooKassa calls, separate database."""
import uuid
from datetime import datetime, timezone

import uvicorn
from fastapi.responses import RedirectResponse

from .config import Settings
from .db import Database
from .provider import ProviderError
from .service import Service
from .web import create_app


class DisabledProvider:
    async def get_payment(self,*args):
        raise ProviderError("Оплата в демонстрации недоступна")

    async def get_refund(self,*args):
        raise ProviderError("Возвраты в демонстрации недоступны")


def build_demo():
    settings = Settings(database_path="data/demo.sqlite3",bot_username="demo_bot")
    db = Database(settings.database_path)
    db.migrate()
    db.seed("events.json")
    service = Service(db,settings,DisabledProvider(),now=lambda:datetime(2026,10,20,tzinfo=timezone.utc))
    oid=uuid.uuid4().hex
    service.create_order(oid,1,"halloween-2026","standard","demo@example.com","Демонстрация","+79991234567")
    import secrets
    token=secrets.token_urlsafe(32)
    with db.transaction() as con:
        con.execute("UPDATE orders SET status='paid',notified=1 WHERE id=?",(oid,))
        con.execute("INSERT INTO tickets(id,order_id,position,token) VALUES(?,?,1,?)",(uuid.uuid4().hex,oid,token))
    app=create_app(service)

    @app.get("/demo")
    async def demo():
        return RedirectResponse("/t/"+token)

    return app


if __name__ == "__main__":
    print("DEMO ONLY: http://127.0.0.1:8000/demo")
    uvicorn.run(build_demo(),host="127.0.0.1",port=8000,access_log=False)
