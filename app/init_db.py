"""本地初始化：建原始明细表 -> 灌虚构数据 -> 发布默认策略 v1。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.default_policy import default_policy
from app.schema import BASE_TABLES
from app.seed import generate
from app.store import Store

_BASE_DDL_TEMPLATES = {
    "customers": """
        customer_id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL,
        email TEXT NOT NULL, id_card TEXT NOT NULL, gender TEXT,
        age INTEGER, city TEXT, register_date TEXT""",
    "addresses": """
        address_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers,
        province TEXT, city TEXT, detail TEXT, is_default INTEGER""",
    "orders": """
        order_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers,
        order_date TEXT, status TEXT, amount REAL, channel TEXT""",
    "order_items": """
        item_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders,
        product_id TEXT NOT NULL REFERENCES products,
        quantity INTEGER, unit_price REAL""",
    "products": """
        product_id TEXT PRIMARY KEY, category TEXT,
        product_name TEXT, brand TEXT""",
}


def init(db_path: str, reset: bool = False) -> dict:
    if reset and os.path.exists(db_path):
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                os.remove(p)
    store = Store(db_path)
    try:
        with store.lock:
            conn = store.connect()
            try:
                for table, cols in BASE_TABLES.items():
                    conn.execute(
                        f"CREATE TABLE IF NOT EXISTS {table} ({_BASE_DDL_TEMPLATES[table]})")
                conn.commit()
                existing = conn.execute(
                    "SELECT COUNT(*) AS n FROM customers").fetchone()["n"]
                if existing == 0:
                    counts = generate(conn)
                else:
                    counts = {t: conn.execute(
                        f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                        for t in BASE_TABLES}
            finally:
                conn.close()
        if store.active_version() is None:
            version = store.publish(default_policy(1))
        else:
            version = store.active_version()
        return {"db": db_path, "rows": counts, "published_version": version}
    finally:
        store.close()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "demo.db")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    print(init(path, reset="--reset" in sys.argv))
