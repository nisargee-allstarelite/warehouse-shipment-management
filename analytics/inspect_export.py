"""Quick look at an export file: which orders have notes, and what tells
auction orders apart from regular TikTok Shop listing orders.
    venv/bin/python3 analytics/inspect_export.py analytics/data/orders_YYYYMMDD.jsonl
"""
import collections, json, sys
from datetime import datetime, timezone

path = sys.argv[1]
n = 0
notes_by_month, orders_by_month = collections.Counter(), collections.Counter()
pname, pname_note, pname_nonote = collections.Counter(), collections.Counter(), collections.Counter()
status_note = collections.Counter()
sample_notes = []
for x in open(path):
    r = json.loads(x); n += 1
    m = datetime.fromtimestamp(int(r["create_time"]), timezone.utc).strftime("%Y-%m")
    orders_by_month[m] += 1
    has = bool((r.get("seller_note") or "").strip())
    names = [(li.get("product_name") or "")[:70] for li in r.get("line_items") or []]
    for nm in set(names):
        pname[nm] += 1
        (pname_note if has else pname_nonote)[nm] += 1
    if has:
        notes_by_month[m] += 1
        status_note[r["status"]] += 1
        if len(sample_notes) < 15:
            sample_notes.append((r["seller_note"][:80], names[:1]))
print(f"orders read: {n}")
print("\nmonth   orders  with_note")
for m in sorted(orders_by_month):
    print(f"{m}  {orders_by_month[m]:7d}  {notes_by_month[m]:7d}")
print("\nstatus of orders WITH a note:", dict(status_note))
print("\nTop 30 listing names overall  (orders | with note | without note):")
for k, v in pname.most_common(30):
    print(f"  {v:6d} | {pname_note[k]:6d} | {pname_nonote[k]:6d}  {k}")
print("\nSample notes:")
for s in sample_notes:
    print("  ", s)
