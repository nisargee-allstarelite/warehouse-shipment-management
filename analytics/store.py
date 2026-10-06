"""
store.py -- local SQLite copy of TikTok order history for Sales Insights.

One row per order (upserted by order_id, so re-fetching recent orders just
updates them: new notes, cancellations, status changes). Buyer personal info
is never stored. File: analytics/data/insights.db (gitignored).
"""
import hashlib
import json
import os
import sqlite3
import threading

DB_PATH = os.environ.get("INSIGHTS_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sales_insights.db")
_lock = threading.Lock()

SCHEMA = """
create table if not exists orders (
  order_id      text primary key,
  create_time   integer,
  status        text,
  order_type    text,
  room_id       text,
  product_name  text,
  lot           text,
  sale_price    real,
  sub_total     real,
  n_items       integer,
  buyer         text,
  state         text,
  seller_note   text
);
create index if not exists orders_time on orders(create_time);
create table if not exists sku_titles (sku text primary key, title text, color text, looked_up_at integer);
create table if not exists meta (k text primary key, v text);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.executescript(SCHEMA)
    return c


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _state(o):
    if o.get("state"):
        return o["state"]
    for lvl in (o.get("recipient_address") or {}).get("district_info") or []:
        if (lvl.get("address_level_name") or "").lower() in ("state", "province", "l1"):
            return lvl.get("address_name")
    return None


def _buyer(o):
    if o.get("buyer"):
        return o["buyer"]
    uid = o.get("user_id") or ""
    return hashlib.sha256(("ase-" + uid).encode()).hexdigest()[:16] if uid else None


def row_from_order(o):
    """Works for both raw TikTok order detail and the export's cleaned rows."""
    lis = o.get("line_items") or []
    li = lis[0] if lis else {}
    pay = o.get("payment") or {}
    return (
        o.get("order_id") or o.get("id"), int(o.get("create_time") or 0), o.get("status"), o.get("order_type"),
        next((x.get("room_id") for x in lis if x.get("room_id")), None),
        li.get("product_name"), li.get("sku_name"),
        sum(_f(x.get("sale_price")) or 0 for x in lis), _f(pay.get("sub_total")), len(lis),
        _buyer(o), _state(o), o.get("seller_note") or "",
    )


def upsert_orders(orders):
    rows = [row_from_order(o) for o in orders if (o.get("order_id") or o.get("id"))]
    with _lock, connect() as c:
        c.executemany("insert or replace into orders values (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def import_jsonl(path, batch=5000):
    n, buf = 0, []
    with open(path) as f:
        for x in f:
            buf.append(json.loads(x))
            if len(buf) >= batch:
                n += upsert_orders(buf); buf = []
    if buf:
        n += upsert_orders(buf)
    return n


def set_meta(k, v):
    with _lock, connect() as c:
        c.execute("insert or replace into meta values (?,?)", (k, str(v)))


def get_meta(k, default=None):
    with connect() as c:
        r = c.execute("select v from meta where k=?", (k,)).fetchone()
    return r[0] if r else default


if __name__ == "__main__":
    import sys
    print("imported", import_jsonl(sys.argv[1]), "orders into", DB_PATH)
