"""
inventory_deduct.py -- Pack & Deduct: subtract packed units from the
All Star Elite Shopify WAREHOUSE location.

Same flow as store-refill's refill_push.py (warehouse side):

preview(lines)
    lines = [{inventory_item_id, product_id, variant_id, style, size, sku,
    barcode, qty}] from the scan page. Reads the LIVE warehouse qty for each
    and returns Current -> New. Same item scanned twice is merged.
    NOTHING is written. The preview is stored server-side under a
    preview_id that can be confirmed ONCE (double-click / second tab /
    browser back + resubmit can't deduct twice).

run(preview_id)
    Phase 1  per line: fresh read, subtract qty with changeFromQuantity =
             the fresh number (Shopify rejects it if stock moved in between)
             + idempotency key. Logged the moment Shopify accepts it.
    Phase 2  wait SETTLE_SECONDS, re-read every line -> 'mismatch' if
             Shopify shows something other than before - qty (a sale or a
             Flow touched it) -- already deducted, do NOT deduct again.

Every line is appended to inventory_log.jsonl with before/after and the
Shopify inventory-history + edit links.
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import shopify_inventory as SI

LOG_FILE = "inventory_log.jsonl"
SETTLE_SECONDS = 10
PREVIEW_TTL_SECONDS = 30 * 60

_lock = threading.Lock()
_previews = {}   # preview_id -> {"created": ts, "lines": [...], "used": bool}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _merge(lines):
    merged = {}
    for l in lines:
        iid = str(l.get("inventory_item_id") or "").strip()
        try:
            qty = int(l.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if not iid.isdigit() or qty <= 0:
            continue
        if iid in merged:
            merged[iid]["qty"] += qty
        else:
            merged[iid] = {k: l.get(k) for k in ("product_id", "variant_id", "style", "size", "sku", "barcode")}
            merged[iid].update(inventory_item_id=iid, qty=qty)
    return list(merged.values())


def preview(lines, note=""):
    client = SI.get_client()
    rows = []
    for l in _merge(lines):
        row = dict(l, current=None, new=None, ok=False, warning=None, error=None,
                   history=SI.history_url(l["inventory_item_id"]),
                   edit=SI.edit_url(l["product_id"], l["variant_id"]) if l.get("product_id") and l.get("variant_id") else None)
        try:
            cur = client.get_qty(l["inventory_item_id"])
            if cur is None:
                row["error"] = "Not stocked at the Warehouse location in Shopify -- nothing will be deducted"
            else:
                row.update(current=cur, new=cur - l["qty"], ok=True)
                if row["new"] < 0:
                    row["warning"] = "Warehouse would go below 0 -- check the count"
        except Exception as e:
            row["error"] = f"Couldn't read Shopify: {e}"
        rows.append(row)
    pid = str(uuid.uuid4())
    with _lock:
        cutoff = time.time() - PREVIEW_TTL_SECONDS
        for k in [k for k, v in _previews.items() if v["created"] < cutoff]:
            _previews.pop(k, None)
        _previews[pid] = {"created": time.time(), "lines": rows, "used": False, "note": (note or "")[:200]}
    return {"preview_id": pid, "rows": rows,
            "n_ok": sum(r["ok"] for r in rows), "n_bad": sum(not r["ok"] for r in rows),
            "units": sum(r["qty"] for r in rows if r["ok"])}


def _log(entry):
    with _lock:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")


def claim(preview_id):
    """Marks a preview as used. Returns its data, or an error string."""
    with _lock:
        p = _previews.get(preview_id)
        if not p:
            return None, "This preview expired or doesn't exist -- scan again and preview again."
        if p["used"]:
            return None, "This preview was already confirmed -- nothing was deducted twice."
        p["used"] = True
        return p, None


def run(p, sleep=time.sleep):
    client = SI.get_client()
    batch_id = str(uuid.uuid4())[:8]
    res = {"batch_id": batch_id, "ok": 0, "units": 0, "failed": 0, "mismatch": 0, "lines": []}
    base = {"batch_id": batch_id, "note": p.get("note", "")}

    def record(row, status, before, after, msg=None):
        e = dict(base, at=_now(), style=row["style"], size=row["size"], sku=row["sku"], barcode=row.get("barcode"),
                 qty=row["qty"], before=before, after=after, status=status, message=msg,
                 inventory_item_id=row["inventory_item_id"], history=row["history"], edit=row["edit"])
        _log(e)
        res["lines"].append(e)

    done = []
    for row in p["lines"]:
        if not row["ok"]:
            res["failed"] += 1
            record(row, "skipped", None, None, row["error"])
            continue
        try:
            fresh = client.get_qty(row["inventory_item_id"])
            if fresh is None:
                raise RuntimeError("No inventory record at the Warehouse location")
            client.adjust(row["inventory_item_id"], -row["qty"], fresh)
        except Exception as e:
            res["failed"] += 1
            record(row, "error", None, None, f"Nothing deducted: {e}"[:400])
            continue
        res["ok"] += 1
        res["units"] += row["qty"]
        record(row, "ok", fresh, fresh - row["qty"])   # units are gone in Shopify -> log now
        done.append((row, res["lines"][-1], fresh - row["qty"]))

    if done:
        sleep(SETTLE_SECONDS)
    for row, line, expected in done:
        try:
            actual = client.get_qty(row["inventory_item_id"])
        except Exception as e:
            actual = f"couldn't re-check ({e})"
        if actual != expected:
            res["mismatch"] += 1
            msg = (f"Deducted, but {SETTLE_SECONDS}s later Shopify shows {actual} instead of {expected} "
                   f"(a sale or a Flow changed it). Do NOT deduct again -- check the history.")[:400]
            line.update(status="mismatch", message=msg)
            _log(dict(line, at=_now(), qty=0, after=actual if isinstance(actual, int) else None,
                      status="mismatch_check"))
    return res


def read_log(search="", limit=500):
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE) as f:
        raw = [json.loads(x) for x in f if x.strip()]
    # fold each 10s re-check result into the line it belongs to
    entries, by_key = [], {}
    for e in raw:
        key = (e.get("batch_id"), str(e.get("inventory_item_id")))
        if e.get("status") == "mismatch_check":
            if key in by_key:
                by_key[key].update(status="mismatch", message=e.get("message"), rechecked=e.get("after"))
            continue
        by_key[key] = e
        entries.append(e)
    entries.reverse()
    s = (search or "").strip().lower()
    if s:
        entries = [e for e in entries if s in " ".join(str(e.get(k) or "") for k in
                   ("style", "size", "sku", "barcode", "batch_id", "note")).lower()]
    return entries[:limit]

