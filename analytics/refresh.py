"""
refresh.py -- keeps the Sales Insights store current. READ-ONLY on TikTok
and Shopify.

refresh(days=14):
  1. Pull every order created in the last `days` days (all statuses) from
     TikTok, with full details (seller notes), and upsert them. Re-reading
     recent days picks up notes added after the sale and status changes.
  2. Look up Shopify product titles/colors for any new SKUs found in notes,
     so styles show real names instead of SKU codes.
Runs inside the dashboard process (shares its TikTok token handling).
"""
import re
import threading
import time
from datetime import datetime, timezone

import tiktok_api
from bucketing import split_multi_item_note, parse_note_lines
from analytics import store

_lock = threading.Lock()
status = {"running": False, "started": None, "finished": None, "message": None, "error": None}


def _fetch_orders(days):
    ids = tiktok_api.get_all_order_ids(days_back=days)
    n = 0
    for i in range(0, len(ids), 500):
        orders = tiktok_api.get_order_details(ids[i:i + 500])
        n += store.upsert_orders(orders)
        status["message"] = f"Orders updated: {n} of {len(ids)}"
    return n, len(ids)


def _all_note_skus(c):
    skus = set()
    for (note,) in c.execute("select seller_note from orders where seller_note != ''"):
        for e in parse_note_lines(split_multi_item_note(note)):
            s = (e.get("sku") or "").strip().upper()
            if s and re.match(r"^[A-Z0-9]+-[A-Z0-9]", s):
                skus.add(s)
    return skus


def lookup_sku_titles(max_new=3000):
    """Shopify (All Star Elite) product title + Color option for each new SKU."""
    import shopify_inventory
    sh = shopify_inventory.get_client()
    if not sh.configured():
        return 0
    c = store.connect()
    known = {r[0] for r in c.execute("select sku from sku_titles")}
    todo = sorted(_all_note_skus(c) - known)[:max_new]
    q = """query($q: String!) { productVariants(first: 100, query: $q) {
             nodes { sku selectedOptions { name value } product { title } } } }"""
    found = 0
    for i in range(0, len(todo), 25):
        batch = todo[i:i + 25]
        qs = " OR ".join('sku:"%s"' % s.replace('"', "") for s in batch)
        try:
            nodes = sh.graphql(q, {"q": qs})["productVariants"]["nodes"]
        except Exception as e:
            status["message"] = f"Shopify lookup issue: {e}"
            continue
        hits = {}
        for n in nodes:
            s = (n.get("sku") or "").upper()
            if s in batch:
                opts = {o["name"].lower(): o["value"] for o in n.get("selectedOptions") or []}
                hits[s] = (n["product"]["title"], opts.get("color"))
        now = int(time.time())
        with c:
            for s in batch:   # misses are stored too, so they aren't re-queried every time
                t, col = hits.get(s, (None, None))
                c.execute("insert or replace into sku_titles values (?,?,?,?)", (s, t, col, now))
        found += len(hits)
    return found


def refresh(days=14):
    if not _lock.acquire(blocking=False):
        return False
    status.update(running=True, started=int(time.time()), error=None, message="Starting...")
    try:
        n, total = _fetch_orders(days)
        status["message"] = "Looking up product names in Shopify..."
        lookup_sku_titles()
        store.set_meta("updated_at", int(time.time()))
        status["message"] = f"Updated {n} orders from the last {days} days"
    except Exception as e:
        status["error"] = str(e)[:300]
    finally:
        status.update(running=False, finished=int(time.time()))
        _lock.release()
    return True


def start_background(days=14):
    if status["running"]:
        return False
    threading.Thread(target=refresh, kwargs={"days": days}, daemon=True).start()
    return True


def nightly_loop(hour_et=5):
    """Runs refresh once a day around `hour_et` (US Eastern)."""
    from zoneinfo import ZoneInfo
    last_day = None
    while True:
        now = datetime.now(ZoneInfo("America/New_York"))
        if now.hour == hour_et and now.date() != last_day:
            last_day = now.date()
            refresh()
        time.sleep(300)
