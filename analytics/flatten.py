"""Flatten the order export into two small CSVs for analysis:
  items.csv  - one row per line item (order + item fields)
  notes.csv  - one row per parsed note line (product/size/color from seller notes)
Times converted to US/Eastern."""
import csv, json, sys
from datetime import datetime
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
src = sys.argv[1]; outdir = sys.argv[2]
IF = ["order_id","order_type","status","create_et","paid_et","buyer","state","n_items","order_total","order_subtotal",
      "seller_discount","platform_discount","has_note","line_id","product_id","product_name","sku_id","sku_name","seller_sku",
      "sale_price","original_price","is_gift","sku_type","room_id","item_status","cancel_reason"]
NF = ["order_id","create_et","room_id","line_no","n_note_lines","bucket_key","name","sku","color","size","qty","raw","order_subtotal","n_items"]
def et(ts):
    return datetime.fromtimestamp(int(ts), ET).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
with open(src) as f, open(f"{outdir}/items.csv","w",newline="") as fi, open(f"{outdir}/notes.csv","w",newline="") as fn:
    wi = csv.DictWriter(fi, IF); wi.writeheader(); wn = csv.DictWriter(fn, NF); wn.writeheader()
    for x in f:
        o = json.loads(x); p = o.get("payment") or {}; lis = o.get("line_items") or []
        base = dict(order_id=o.get("order_id"), order_type=o.get("order_type"), status=o.get("status"),
                    create_et=et(o.get("create_time")), paid_et=et(o.get("paid_time")), buyer=o.get("buyer"),
                    state=o.get("state"), n_items=len(lis), order_total=p.get("total_amount"), order_subtotal=p.get("sub_total"),
                    seller_discount=p.get("seller_discount"), platform_discount=p.get("platform_discount"),
                    has_note=int(bool((o.get("seller_note") or "").strip())))
        room = next((li.get("room_id") for li in lis if li.get("room_id")), "")
        for li in lis:
            wi.writerow(dict(base, line_id=li.get("id"), product_id=li.get("product_id"), product_name=li.get("product_name"),
                sku_id=li.get("sku_id"), sku_name=li.get("sku_name"), seller_sku=li.get("seller_sku"), sale_price=li.get("sale_price"),
                original_price=li.get("original_price"), is_gift=li.get("is_gift"), sku_type=li.get("sku_type"),
                room_id=li.get("room_id") or "", item_status=li.get("display_status"), cancel_reason=li.get("cancel_reason")))
        ni = o.get("note_items") or []
        for k, e in enumerate(ni):
            wn.writerow(dict(order_id=o.get("order_id"), create_et=base["create_et"], room_id=room, line_no=k, n_note_lines=len(ni),
                             order_subtotal=p.get("sub_total"), n_items=len(lis), **{c: e.get(c) for c in ("bucket_key","name","sku","color","size","qty","raw")}))
print("done")
