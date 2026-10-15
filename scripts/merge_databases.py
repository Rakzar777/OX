"""Merge a stopped bot's SQLite backup without replacing current orders.

Run without --apply first. Conflicting orders abort the whole transaction.
Both bot instances must be stopped when applying; keep the source as a backup.
"""
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


TABLES = ("users", "events", "ticket_types", "orders", "order_items", "payments", "tickets", "refunds")
KEYS = {t: "id" for t in TABLES}
KEYS.update(order_items="order_id", payments="order_id", refunds="order_id")


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def validate(con):
    if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("Database integrity check failed")
    if con.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("Database has broken references")
    return {t: [r[1] for r in con.execute(f"PRAGMA table_info({t})")] for t in TABLES}


def merge_databases(source, target, apply=False):
    if Path(source).resolve() == Path(target).resolve():
        raise ValueError("Source and destination must differ")
    with readonly(source) as src, readonly(target) as before:
        schema = validate(src)
        if schema != validate(before) or any(not cols for cols in schema.values()):
            raise ValueError("Database schemas differ")
        # Simulate on an in-memory copy by default; never write during a dry run.
        dst = sqlite3.connect(target if apply else ":memory:")
        try:
            if apply:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup = Path(str(target) + ".before-merge-" + stamp)
                with sqlite3.connect(backup) as saved:
                    before.backup(saved)
                backup.chmod(0o600)
            else:
                before.backup(dst)
            dst.execute("PRAGMA foreign_keys=ON")
            dst.execute("BEGIN IMMEDIATE")
            counts = {}
            for table in TABLES:
                columns = schema[table]
                key = KEYS[table]
                count = 0
                for row in src.execute(f"SELECT * FROM {table}"):
                    existing = dst.execute(f"SELECT * FROM {table} WHERE {key}=?",
                                           (row[columns.index(key)],)).fetchone()
                    if existing is not None:
                        # Keep the destination's current event catalog and prices.
                        if table not in ("users", "events", "ticket_types") and existing != row:
                            raise ValueError(f"Conflicting {table} record; merge aborted")
                        continue
                    placeholders = ",".join("?" for _ in columns)
                    dst.execute(f"INSERT INTO {table} VALUES({placeholders})", row)
                    count += 1
                counts[table] = count
            validate(dst)
            dst.commit()
            return {"applied": apply, "inserted": counts,
                    "orders": dict(dst.execute("SELECT status,count(*) FROM orders GROUP BY status")),
                    "tickets": dict(dst.execute("SELECT status,count(*) FROM tickets GROUP BY status"))}
        finally:
            dst.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("target")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(merge_databases(args.source, args.target, args.apply)))
