"""
dataset.py -- turns stored orders into the rows the Sales Insights page uses.

Only orders WITH a seller note (product info) are analyzed; cancelled orders
are excluded. One row per note line (= one product unit in the lot). The lot's
sale price is split evenly across its note lines.
"""
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import Counter

from bucketing import split_multi_item_note, parse_note_lines, NEEDS_REVIEW
from analytics import store

GENERIC = re.compile(r"^\s*(SHOE|SHOES|SNEAKER|SNEAKERS|BOTTOM|BOTTOMS|TOP|TOPS|SWEATPANT|SWEATPANTS|HAT|ACCESSORY)\s*:", re.I)
GENERIC_CAT = {"SHOE": "Sneakers (unspecified)", "SHOES": "Sneakers (unspecified)", "SNEAKER": "Sneakers (unspecified)",
               "SNEAKERS": "Sneakers (unspecified)", "BOTTOM": "Bottoms (unspecified)", "BOTTOMS": "Bottoms (unspecified)",
               "TOP": "Tops (unspecified)", "TOPS": "Tops (unspecified)", "SWEATPANT": "Sweatpants",
               "SWEATPANTS": "Sweatpants", "HAT": "Hats", "ACCESSORY": "Accessories (unspecified)"}
SIZE_TOKEN = re.compile(r"^(XXS|XS|S|M|L|XL|2XL|3XL|4XL|5XL|XXL|XXXL|OS|\d{1,2}(\.5)?)$", re.I)
UNSPEC = "Unspecified style"
ET = ZoneInfo("America/New_York")


def _title(s):
    s = re.sub(r"\s+", " ", (s or "").strip())
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)          # trailing (COLOR)
    s = re.sub(r"^watson\s+", "", s, flags=re.I)      # "Watson Vulcan Sneakers" == "Vulcan Sneakers"
    return s.title() if s.isupper() or s.islower() else s


def _sku_base(sku):
    parts = [p for p in sku.split("-") if p]
    return "-".join(parts[:3]).upper() if len(parts) >= 3 else sku.upper()


SIZE_FIX = {"2X": "2XL", "3X": "3XL", "4X": "4XL", "XXL": "2XL", "XXXL": "3XL"}


def norm_size(z):
    if not z:
        return None
    z = z.strip().upper().rstrip(")")
    z = SIZE_FIX.get(z, z)
    if re.fullmatch(r"0?\d{1,2}(\.5)?", z):
        f = float(z)
        if not (4 <= f <= 16 or 24 <= f <= 50):   # shoe or waist sizes only
            return None
        z = str(f).rstrip("0").rstrip(".")
    return z if SIZE_TOKEN.match(z) else None   # "M/L/XL", "L,XL,2XL" etc. -> no single size


def norm_color(c, sku=""):
    """Drops things the note parser mistook for a color: quantities, SKU
    category codes (FBJ, BBJ, TSHT...), sizes, bare numbers."""
    if not c:
        return None
    c = re.sub(r"\s+", " ", c.strip().upper())
    parts = [p.upper() for p in (sku or "").split("-")]
    if (re.search(r"\d|QTY", c) or SIZE_TOKEN.match(c) or (len(parts) > 1 and c in parts[1:2])
            or c in ("BBJ", "BAJ", "FBJ", "TSHT", "HOOD", "SWEAT", "SHT")):
        return None
    return c


def classify(e, titles):
    """-> category, style, size, color for one parsed note line."""
    raw = e.get("raw") or ""
    cat = e.get("bucket_key") or "Other"
    size = (e.get("size") or "").strip().upper() or None
    color = (e.get("color") or "").strip().upper() or None
    sku = (e.get("sku") or "").strip()
    m = GENERIC.match(raw)
    if m:
        cat = GENERIC_CAT.get(m.group(1).upper(), cat)
        rest = raw[m.end():].strip()
        return cat, UNSPEC, norm_size(rest if SIZE_TOKEN.match(rest) else size), None
    if sku:
        last = sku.split("-")[-1]
        if SIZE_TOKEN.match(last):
            size = last.upper()
        t = titles.get(sku.upper())
        style = _title(t[0]) if t and t[0] else _sku_base(sku)
        if t and t[1] and not color:
            color = t[1].upper()
    else:
        style = _title(re.sub(r"\s*[-—]\s*SIZE.*$", "", e.get("name") or "", flags=re.I)) or UNSPEC
    if cat in ("Others", NEEDS_REVIEW):
        cat = "Other"
    return cat, style, norm_size(size), norm_color(color, sku)


def build():
    c = store.connect()
    titles = {r[0]: (r[1], r[2]) for r in c.execute("select sku, title, color from sku_titles")}
    total = c.execute("select count(*) from orders where status != 'CANCELLED' and order_type = 'AUCTION'").fetchone()[0]
    totals_by_date = Counter()
    for (t,) in c.execute("select create_time from orders where status != 'CANCELLED' and order_type = 'AUCTION'"):
        totals_by_date[datetime.fromtimestamp(t, ET).strftime("%Y-%m-%d")] += 1
    q = c.execute("""select order_id, create_time, room_id, sale_price, seller_note from orders
                     where seller_note != '' and status != 'CANCELLED' and order_type = 'AUCTION'""")
    rows, n_orders = [], 0
    for oid, t, room, price, note in q:
        lines = split_multi_item_note(note)
        parsed = [e for e in parse_note_lines(lines)] if lines else []
        if not parsed:
            continue
        n_orders += 1
        units = [max(int(e.get("qty") or 1), 1) for e in parsed]
        tot_units = sum(units)
        d = datetime.fromtimestamp(t, ET)
        for e, u in zip(parsed, units):
            cat, style, size, color = classify(e, titles)
            rows.append([oid, d.strftime("%Y-%m-%d"), d.weekday(), d.hour, cat, style, size, color, u,
                         round((price or 0) * u / tot_units, 2), round(price or 0, 2), len(parsed), room])
    return {
        "cols": ["order", "date", "dow", "hour", "category", "style", "size", "color", "units", "revenue", "lot_price", "lines", "room"],
        "rows": rows,
        "orders_analyzed": n_orders,
        "orders_total": total,
        "totals_by_date": dict(sorted(totals_by_date.items())),
        "updated_at": store.get_meta("updated_at"),
        "built_at": int(time.time()),
    }


if __name__ == "__main__":
    d = build()
    print(d["orders_analyzed"], "of", d["orders_total"], "orders;", len(d["rows"]), "rows")
    st = Counter((r[4], r[5]) for r in d["rows"]); print(len(st), "styles"); print(st.most_common(15))
