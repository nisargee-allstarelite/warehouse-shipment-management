"""
Bulk shipping automation - purchases real labels via TikTok Shop's fulfillment API.

This is the exact 3-step pipeline manually verified end-to-end on real orders:
  1. Create Packages  - once per order (no batch version exists for this step)
  2. Batch Ship Packages - one single call covering every package created above
  3. Get Package Shipping Document - once per package (no batch version for this either)

IMPORTANT: every call here is REAL. Create Packages genuinely purchases a label
and spends real money the moment it succeeds. There is no dry-run mode.
"""

import os
import time
import requests
from io import BytesIO
from pypdf import PdfReader, PdfWriter
from tiktok_api import make_request, VERSION

DOCUMENT_TYPE = "SHIPPING_LABEL_AND_PACKING_SLIP"
HANDOVER_METHOD = "PICKUP"
LABELS_DIR = "labels"


def create_package(order_id):
    """
    Step 1. Purchases a label for a single order using TikTok's own defaults
    for weight/dimensions/shipping service (we deliberately don't override
    these - verified this works correctly against real orders).

    Returns (success, result) where result is either the response data
    (containing package_id, shipping_service_info, etc.) or an error dict.
    """
    path = f"/fulfillment/{VERSION}/packages"
    data = make_request("POST", path, body={"order_id": order_id}, quiet=True)

    if data.get("code") == 0:
        return True, data["data"]
    return False, {
        "code": data.get("code"),
        "message": data.get("message", "Unknown error"),
    }


def batch_ship_packages(package_ids, handover_method=HANDOVER_METHOD):
    """
    Step 2. Ships every package_id in ONE call. Response only lists failures -
    any package_id not present in the returned errors list succeeded.

    Returns (succeeded_ids, failed) where failed is a dict of
    package_id -> {code, message} for anything that failed.
    """
    if not package_ids:
        return [], {}

    path = f"/fulfillment/{VERSION}/packages/ship"
    body = {
        "packages": [
            {"id": pid, "handover_method": handover_method} for pid in package_ids
        ]
    }
    data = make_request("POST", path, body=body, quiet=True)

    if data.get("code") != 0:
        # The whole batch call itself failed - treat every package as failed
        failed = {
            pid: {"code": data.get("code"), "message": data.get("message", "Batch ship call failed")}
            for pid in package_ids
        }
        return [], failed

    errors = data.get("data", {}).get("errors", [])
    failed = {}
    for err in errors:
        pid = err.get("detail", {}).get("package_id")
        if pid:
            failed[pid] = {"code": err.get("code"), "message": err.get("message")}

    succeeded_ids = [pid for pid in package_ids if pid not in failed]
    return succeeded_ids, failed


def get_shipping_document(package_id):
    """
    Step 3. Retrieves the label + packing slip URL for an already-shipped
    package. The URL TikTok returns is only valid for 24 hours.

    Returns (success, result) where result is either {doc_url, tracking_number}
    or an error dict.
    """
    path = f"/fulfillment/{VERSION}/packages/{package_id}/shipping_documents"
    data = make_request(
        "GET", path, query_params={"document_type": DOCUMENT_TYPE}, quiet=True
    )

    if data.get("code") == 0:
        return True, data["data"]
    return False, {
        "code": data.get("code"),
        "message": data.get("message", "Unknown error"),
    }


def ship_orders(order_ids, progress_callback=None):
    """
    Runs the full pipeline for a list of order_ids and returns one result dict
    per order, regardless of where it succeeded or failed:

    {
        "order_id": "...",
        "success": True/False,
        "stage": "create_package" | "ship" | "get_document" | None (None if fully successful),
        "error": "..." (only present if success is False),
        "package_id": "...",           (present once created)
        "tracking_number": "...",      (present once shipped + document retrieved)
        "doc_url": "...",              (present once document retrieved)
        "shipping_price": "...",       (present once created)
    }

    Every order gets exactly one result. Orders that fail at any stage stop
    there for that order (we never retry create_package automatically, since
    that would risk purchasing a second label for the same order).
    """

    def report(msg):
        if progress_callback:
            progress_callback(msg)

    results = {oid: {"order_id": oid, "success": False} for oid in order_ids}

    # --- Step 1: Create Packages, one call per order ---
    order_to_package = {}
    for oid in order_ids:
        report(f"Creating package for order {oid}...")
        success, data = create_package(oid)
        if success:
            pkg_id = data["package_id"]
            order_to_package[oid] = pkg_id
            results[oid]["package_id"] = pkg_id
            svc = data.get("shipping_service_info", {})
            results[oid]["shipping_price"] = svc.get("price")
            results[oid]["shipping_service_name"] = svc.get("name")
        else:
            results[oid]["stage"] = "create_package"
            results[oid]["error"] = data.get("message", "Failed to create package")
        time.sleep(0.2)  # small pacing buffer between real purchase calls

    package_ids = list(order_to_package.values())
    if not package_ids:
        return list(results.values())

    # --- Step 2: Batch Ship, one call for everything created above ---
    report(f"Shipping {len(package_ids)} package(s) in one batch call...")
    succeeded_pkg_ids, failed_pkgs = batch_ship_packages(package_ids)

    package_to_order = {v: k for k, v in order_to_package.items()}
    for pkg_id, err in failed_pkgs.items():
        oid = package_to_order.get(pkg_id)
        if oid:
            results[oid]["stage"] = "ship"
            results[oid]["error"] = err.get("message", "Failed to ship package")

    # --- Step 3: Get Document, one call per successfully-shipped package ---
    for pkg_id in succeeded_pkg_ids:
        oid = package_to_order.get(pkg_id)
        if not oid:
            continue
        report(f"Retrieving label for order {oid}...")
        success, data = get_shipping_document(pkg_id)
        if success:
            results[oid]["success"] = True
            results[oid]["doc_url"] = data.get("doc_url")
            results[oid]["tracking_number"] = data.get("tracking_number")
        else:
            results[oid]["stage"] = "get_document"
            results[oid]["error"] = data.get("message", "Failed to retrieve label")
        time.sleep(0.2)

    return list(results.values())


def build_combined_label_pdf(results, bucket_key):
    """
    Downloads every successfully-generated label PDF and merges them into
    ONE multi-page PDF - so the ops manager can open a single file and print
    everything for this batch in one go, instead of clicking N separate links.

    Returns (filename, included_orders, skipped_orders). filename is relative
    to LABELS_DIR, or None if there was nothing successful to combine. Orders
    whose individual PDF fails to download are skipped (noted in the skip
    list) - this never blocks the rest of the batch from being combined.
    """
    os.makedirs(LABELS_DIR, exist_ok=True)

    writer = PdfWriter()
    included_orders = []
    skipped_orders = []

    for r in results:
        if not r.get("success") or not r.get("doc_url"):
            continue
        try:
            resp = requests.get(r["doc_url"], timeout=15)
            resp.raise_for_status()
            reader = PdfReader(BytesIO(resp.content))
            for page in reader.pages:
                writer.add_page(page)
            included_orders.append(r["order_id"])
        except Exception as e:
            skipped_orders.append({"order_id": r["order_id"], "error": str(e)})

    if not included_orders:
        return None, included_orders, skipped_orders

    safe_key = "".join(c if c.isalnum() else "_" for c in bucket_key)[:40]
    filename = f"labels_{safe_key}_{int(time.time())}.pdf"
    filepath = os.path.join(LABELS_DIR, filename)

    with open(filepath, "wb") as f:
        writer.write(f)

    return filename, included_orders, skipped_orders