"""
export_order_history.py -- READ-ONLY pull of TikTok Shop order history for
the Sales Insights analysis. Writes nothing to TikTok.

Run on the server from the repo folder:
    venv/bin/python3 -u analytics/export_order_history.py            # last 365 days
    venv/bin/python3 -u analytics/export_order_history.py --days 730

Output: analytics/data/orders_<date>.jsonl  (one order per line) + a summary.

Privacy: buyer name, email, nickname, avatar, message and street address are
DROPPED. Kept: state, and a one-way hashed buyer id (repeat buyers, not identity).

Token safety: this script NEVER refreshes the TikTok token (refreshing here
would rotate the refresh token and break the running dashboard). If the
token is expired it stops and asks you to restart the dashboard service,
which refreshes it properly.
"""
import argparse
import collections
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tiktok_api as T  # noqa: E402
from bucketing import split_multi_item_note, parse_note_lines, NEEDS_REVIEW  # noqa: E402


def _no_refresh(*a, **k):
    raise SystemExit("TikTok token needs a refresh. Run: sudo systemctl restart tiktok-dashboard "
                     "(it refreshes the token safely), wait 1 minute, then run this again.")


T._ensure_fresh_token = lambda: None
T.refresh_access_token = _no_refresh

PAGE_SIZE = 100
WINDOW_DAYS = 15


def call(method, path, query=None, body=None):
    for attempt in range(5):
        d = T.make_request(method, path, query_params=query, body=body, quiet=True)
        if d.get("code") == 0:
            return d.get("data") or {}
        if d.get("code") == 105002:
            _no_refresh()
        print(f"  API error (try {attempt + 1}/5): {d.get('code')} {d.get('message')}")
        time.sleep(2 * (attempt + 1))
    raise SystemExit("Stopping: TikTok kept returning errors (see above). Nothing was changed.")


def search_pages(ge, lt):
    """Yields one page (up to 100 orders) at a time, so memory stays small."""
    path = f"/order/{T.VERSION}/orders/search"
    token = None
    while True:
        q = {"page_size": PAGE_SIZE, "sort_field": "create_time", "sort_order": "ASC"}
        if token:
            q["page_token"] = token
        d = call("POST", path, q, {"create_time_ge": ge, "create_time_lt": lt})
        orders = d.get("orders") or []
        if orders:
            yield orders
        token = d.get("next_page_token")
        if not token or not orders:
            return


def details(ids):
    path = f"/order/{T.VERSION}/orders"
    out = []
    for i in range(0, len(ids), 50):
        d = call("GET", path, {"ids": ",".join(ids[i:i + 50])})
        out.extend(d.get("orders") or [])
    return out


def state_of(addr):
    for lvl in (addr or {}).get("district_info") or []:
        if (lvl.get("address_level_name") or "").lower() in ("state", "province", "l1"):
            return lvl.get("address_name")
    return None


PII_FIELDS = ("recipient_address", "buyer_email", "buyer_nickname", "buyer_avatar", "buyer_message", "user_id")


def clean(o):
    """Keeps EVERY field TikTok returns (order_type, room_id, sku_name, ...)
    except buyer personal info. Adds state, hashed buyer id and parsed note."""
    lines = split_multi_item_note(o.get("seller_note", ""))
    parsed = parse_note_lines(lines) if lines else []
    uid = o.get("user_id") or ""
    r = {k: v for k, v in o.items() if k not in PII_FIELDS}
    for li in r.get("line_items") or []:
        li.pop("sku_image", None)
    r["order_id"] = o.get("id")
    r["seller_note"] = o.get("seller_note", "")
    r["buyer"] = hashlib.sha256(("ase-" + uid).encode()).hexdigest()[:16] if uid else None
    r["state"] = state_of(o.get("recipient_address"))
    r["note_items"] = [{k: e.get(k) for k in ("bucket_key", "name", "sku", "color", "size", "qty", "raw")}
                       for e in parsed]
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--summary-only", help="path of an existing export to summarize")
    a = ap.parse_args()
    if a.summary_only:
        return summarize(a.summary_only)
    now = int(time.time())
    start = now - a.days * 86400
    os.makedirs("analytics/data", exist_ok=True)
    out_path = f"analytics/data/orders_{datetime.now().strftime('%Y%m%d')}.jsonl"

    need_details = None
    seen, n_written, raw_keys = set(), 0, set()
    with open(out_path, "w") as f:
        ge = start
        while ge < now:
            lt = min(ge + WINDOW_DAYS * 86400, now + 1)
            n_window = 0
            for orders in search_pages(ge, lt):
                if need_details is None:
                    raw_keys = set(orders[0].keys())
                    need_details = "seller_note" not in raw_keys
                    if need_details:
                        print("  (search results don't include seller notes - fetching full order details too)")
                if need_details:
                    orders = details([o["id"] for o in orders])
                for o in orders:
                    if o.get("id") in seen:
                        continue
                    seen.add(o.get("id"))
                    f.write(json.dumps(clean(o)) + "\n")
                    n_written += 1
                    n_window += 1
            print(f"{datetime.fromtimestamp(ge, timezone.utc):%Y-%m-%d} -> "
                  f"{datetime.fromtimestamp(lt, timezone.utc):%Y-%m-%d}: {n_window} orders (total {n_written})")
            ge = lt
    print(f"\nSaved {n_written} orders to {out_path}")
    print("Fields TikTok returned:", ", ".join(sorted(raw_keys)))
    summarize(out_path)


def summarize(path):
    # streamed line by line -- the server has little memory
    st, months, cats = collections.Counter(), collections.Counter(), collections.Counter()
    pnames, skus = collections.Counter(), collections.Counter()
    otype, otype_note, rooms = collections.Counter(), collections.Counter(), set()
    with_note = parsed_ok = with_seller_sku = multi_cat = n = 0
    for x in open(path):
        r = json.loads(x)
        n += 1
        st[r["status"]] += 1
        otype[r.get("order_type")] += 1
        if (r.get("seller_note") or "").strip():
            otype_note[r.get("order_type")] += 1
        for li in r.get("line_items") or []:
            if li.get("room_id"):
                rooms.add(li["room_id"])
        months[datetime.fromtimestamp(int(r["create_time"]), timezone.utc).strftime("%Y-%m")] += 1
        if (r["seller_note"] or "").strip():
            with_note += 1
        good = [e for e in r["note_items"] if e["bucket_key"] != NEEDS_REVIEW]
        if good:
            parsed_ok += 1
            for e in good:
                cats[e["bucket_key"]] += int(e.get("qty") or 1)
            if len({e["bucket_key"] for e in good}) > 1:
                multi_cat += 1
        for li in r["line_items"]:
            pnames[(li.get("product_name") or "")[:60]] += 1
            if li.get("seller_sku"):
                skus["has"] += 1
        if any(li.get("seller_sku") for li in r["line_items"]):
            with_seller_sku += 1
    if not n:
        print("No orders found.")
        return
    pct = lambda x: f"{x} ({100 * x / n:.0f}%)"
    print("\n========== SUMMARY ==========")
    print(f"Orders: {n}   first {min(months)}  last {max(months)}")
    print("By status:", dict(st.most_common()))
    print("By month:", dict(sorted(months.items())))
    print("By order type (orders | with note):", {k: (v, otype_note[k]) for k, v in otype.most_common()})
    print(f"Distinct LIVE rooms (streams): {len(rooms)}")
    print(f"With a seller note: {pct(with_note)}")
    print(f"Note matched to a product/category: {pct(parsed_ok)}   (2+ categories in one order: {multi_cat})")
    print(f"Has a seller SKU on the listing (direct listings): {pct(with_seller_sku)}")
    print("\nTop 25 categories from notes (units):")
    for k, v in cats.most_common(25):
        print(f"  {v:6d}  {k}")
    print("\nTop 25 listing names (line items):")
    for k, v in pnames.most_common(25):
        print(f"  {v:6d}  {k}")


if __name__ == "__main__":
    main()
