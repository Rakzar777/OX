import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY);
CREATE TABLE users(id INTEGER PRIMARY KEY);
CREATE TABLE events(id TEXT PRIMARY KEY, data TEXT NOT NULL, enabled INTEGER NOT NULL);
CREATE TABLE ticket_types(id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id), data TEXT NOT NULL);
CREATE TABLE orders(
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), event_id TEXT NOT NULL REFERENCES events(id),
 snapshot TEXT NOT NULL, email TEXT NOT NULL, full_name TEXT NOT NULL, phone TEXT NOT NULL,
 amount INTEGER NOT NULL CHECK(amount>0), quantity INTEGER NOT NULL CHECK(quantity>0),
 status TEXT NOT NULL DEFAULT 'new', created_at REAL NOT NULL, notified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE order_items(order_id TEXT PRIMARY KEY REFERENCES orders(id), tariff_id TEXT NOT NULL,
 name TEXT NOT NULL, quantity INTEGER NOT NULL, amount INTEGER NOT NULL);
CREATE TABLE payments(order_id TEXT PRIMARY KEY REFERENCES orders(id), id TEXT UNIQUE,
 idem_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'creating',
 url TEXT, created_at REAL NOT NULL);
CREATE TABLE tickets(id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
 position INTEGER NOT NULL, token TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'active',
 used_at REAL, UNIQUE(order_id,position));
CREATE TABLE refunds(order_id TEXT PRIMARY KEY REFERENCES orders(id), id TEXT UNIQUE,
 idem_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'creating',
 reason TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE promo_codes(code TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0);
CREATE INDEX tickets_order ON tickets(order_id);
CREATE INDEX orders_user ON orders(user_id);
INSERT INTO schema_migrations VALUES(1);
"""


class Database:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=15000")
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def transaction(self):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise

    def migrate(self):
        with self.connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)")
            if not con.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone():
                con.executescript("BEGIN IMMEDIATE;" + SCHEMA + "COMMIT;")

    def one(self, sql, args=()):
        with self.connect() as con:
            row = con.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql, args=()):
        with self.connect() as con:
            return [dict(r) for r in con.execute(sql, args).fetchall()]

    def seed(self, file):
        events = json.loads(Path(file).read_text(encoding="utf-8-sig"))
        seen = set()
        for e in events:
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,24}", e["id"]) or e["id"] in seen:
                raise ValueError("Некорректный или повторный id мероприятия")
            seen.add(e["id"])
            start, close = (datetime.fromisoformat(e[k]) for k in ("starts_at", "sales_close_at"))
            if not start.tzinfo or not close.tzinfo or close <= start:
                raise ValueError("Нужны даты с часовым поясом; закрытие продаж после начала")
            tariffs = set()
            for t in e["tariffs"]:
                if not re.fullmatch(r"[a-zA-Z0-9_-]{1,20}", t["id"]) or t["id"] in tariffs:
                    raise ValueError("Некорректный или повторный id тарифа")
                tariffs.add(t["id"])
                price = Decimal(str(t["price"])) * 100
                if price <= 0 or price != int(price) or not isinstance(t["quantity"], int) or t["quantity"] < 1:
                    raise ValueError("Некорректная цена или количество билетов")
        with self.transaction() as con:
            con.execute("UPDATE events SET enabled=0")
            for e in events:
                con.execute("INSERT INTO events VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,enabled=excluded.enabled",
                            (e["id"], json.dumps(e, ensure_ascii=False), int(e.get("enabled", True))))
                for t in e["tariffs"]:
                    con.execute("INSERT INTO ticket_types VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                                (e["id"] + ":" + t["id"], e["id"], json.dumps(t, ensure_ascii=False)))
