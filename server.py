"""
Live Awaiting-Shipment Bucket Dashboard

Runs a background poller that pulls your current Awaiting Shipment queue
from TikTok every POLL_INTERVAL_SECONDS, buckets it by product/style, and
serves a live dashboard at http://localhost:5000

Run with: python3 server.py
Leave the terminal window open - closing it stops the poller.
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request, render_template, send_from_directory, session, redirect, url_for
from dotenv import load_dotenv

import tiktok_api
from bucketing import bucket_orders
from shipping import ship_orders, build_combined_label_pdf, log_shipping_results, get_shipping_history, reconcile_failed_orders, LABELS_DIR

load_dotenv()

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", 300))
PORT = int(os.environ.get("PORT", 5000))
STATE_FILE = "state.json"
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")

if not DASHBOARD_PASSWORD:
    raise RuntimeError("DASHBOARD_PASSWORD is not set in .env - the site cannot start without it.")
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY is not set in .env - the site cannot start without it.")


@app.before_request
def require_login():
    """Every route needs a valid session, except the login page itself and
    static assets. This is a single shared password, not individual
    accounts - good enough to keep the public internet out, not meant as
    strong multi-user security."""
    if request.endpoint in ("login", "static"):
        return
    if not session.get("authenticated"):
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("index"))
        error = "Incorrect password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


state_lock = threading.Lock()
state = {
    "buckets": {},        # bucket_key -> list of item dicts
    "merged_into": {},    # old_key -> new_key, for manual merges
    "last_updated": None,
    "last_error": None,
    "total_orders": 0,
    "is_polling": False,
}


# --- Async job tracking for long-running operations (ship / reconcile) ---
#
# Both shipping a bucket and reconciling failed orders make one real network
# call to TikTok PER ORDER (Create Package, Get Document, etc.) - for even a
# moderate bucket (30-40 orders) this can genuinely take 30-60+ seconds,
# which is long enough for Nginx's proxy timeout to give up and hand the
# browser an HTML error page instead of the real JSON response. When that
# happened for real on a 31-order Tshirts bucket, the frontend saw the
# error, re-enabled its button, and the person (reasonably) clicked "Ship
# all" again - except the FIRST attempt was still running successfully in
# the background the whole time, so the second attempt hit TikTok with the
# same order IDs and got rejected as "already shipped." Nothing actually
# failed, but it looked like total chaos in Shipping History.
#
# Fix: the start endpoint returns a job_id INSTANTLY (before doing any real
# work) and the frontend polls a separate, always-fast status endpoint
# instead of waiting on one long request. Neither request type can ever be
# the slow one, so neither can ever time out. On top of that,
# active_ship_buckets blocks a second concurrent shipping job for the same
# bucket outright - even a confused double-click can no longer trigger the
# duplicate-attempt cascade, independent of any frontend timing.
jobs_lock = threading.Lock()
jobs = {}                      # job_id -> {"status": "running"|"done", "response": {...} or None}
active_ship_buckets = set()    # bucket_keys currently mid-ship
reconcile_state = {"in_progress": False}


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                saved = json.load(f)
                state["buckets"] = saved.get("buckets", {})
                state["merged_into"] = saved.get("merged_into", {})
                state["last_updated"] = saved.get("last_updated")
                state["total_orders"] = saved.get("total_orders", 0)
        except Exception as e:
            print(f"Could not load saved state: {e}")


def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({
            "buckets": state["buckets"],
            "merged_into": state["merged_into"],
            "last_updated": state["last_updated"],
            "total_orders": state["total_orders"],
        }, f, indent=2)


def apply_merges(buckets):
    """Fold any manually-merged bucket keys together."""
    merged = dict(buckets)
    for old_key, new_key in state["merged_into"].items():
        if old_key in merged:
            items = merged.pop(old_key)
            merged.setdefault(new_key, [])
            merged[new_key].extend(items)
    return merged


def poll_once():
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] Polling TikTok for current Awaiting Shipment queue...")
    with state_lock:
        state["is_polling"] = True
        state["last_error"] = None

    try:
        orders = tiktok_api.fetch_awaiting_shipment_orders(days_back=90)
        buckets = bucket_orders(orders)
        buckets = apply_merges(buckets)

        with state_lock:
            state["buckets"] = buckets
            # total_orders reflects only ACTIONABLE orders - ones that have a
            # seller note and got placed in a real bucket. An Awaiting
            # Shipment order with no note yet isn't ready to process, so it
            # shouldn't count toward the number shown at the top of the
            # dashboard (bucket_orders already silently skips these).
            state["total_orders"] = sum(len(v) for v in buckets.values())
            # Timezone-aware timestamp - a naive one here gets misread by
            # the browser's Date parser as local time instead of UTC,
            # which is what caused the "-14245s ago" display bug.
            state["last_updated"] = datetime.now(timezone.utc).isoformat()
            state["is_polling"] = False
            save_state()

        print(f"  Done - {len(orders)} orders across {len(buckets)} buckets.")
    except Exception as e:
        print(f"  ERROR during poll: {e}")
        with state_lock:
            state["last_error"] = str(e)
            state["is_polling"] = False


def poll_loop():
    while True:
        poll_once()
        time.sleep(POLL_INTERVAL_SECONDS)


@app.route("/")
def index():
    return render_template("index.html", poll_interval=POLL_INTERVAL_SECONDS)


@app.route("/history")
def history_page():
    return render_template("history.html")


@app.route("/api/buckets")
def api_buckets():
    with state_lock:
        buckets_summary = [
            {"key": k, "count": len(v), "items": v}
            for k, v in state["buckets"].items()
        ]
        buckets_summary.sort(key=lambda b: -b["count"])

        return jsonify({
            "buckets": buckets_summary,
            "total_orders": state["total_orders"],
            "total_buckets": len(state["buckets"]),
            "last_updated": state["last_updated"],
            "is_polling": state["is_polling"],
            "last_error": state["last_error"],
            "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    threading.Thread(target=poll_once, daemon=True).start()
    return jsonify({"status": "refresh started"})


@app.route("/api/merge", methods=["POST"])
def api_merge():
    """Manually merge one bucket into another (fixes typo-variant splits)."""
    data = request.get_json()
    from_key = data.get("from_key")
    into_key = data.get("into_key")

    if not from_key or not into_key or from_key == into_key:
        return jsonify({"error": "Invalid keys"}), 400

    with state_lock:
        if from_key in state["buckets"]:
            items = state["buckets"].pop(from_key)
            state["buckets"].setdefault(into_key, [])
            state["buckets"][into_key].extend(items)
            state["merged_into"][from_key] = into_key
            save_state()

    return jsonify({"status": "merged"})


@app.route("/api/ship_bucket", methods=["POST"])
def api_ship_bucket():
    """
    Starts shipping REAL labels for orders in the given bucket, using the
    verified 3-step pipeline (Create Packages -> Batch Ship -> Get Document).
    This spends real money the moment it runs - the frontend is responsible
    for confirming with the person before calling this endpoint.

    By default ships every order currently in the bucket. If the request
    body includes "order_ids" (a specific list, from checkbox multi-select
    in the UI), only those orders are shipped instead - everything else in
    the bucket is left alone.

    Returns a job_id IMMEDIATELY - the actual shipping work (one real
    network call per order, per step) happens in a background thread and
    can take a while for larger buckets. Poll /api/job_status/<job_id> for
    the result. Rejects the request outright with 409 if a shipping job for
    this exact bucket is already running, to make double-submission
    (accidental double-click, or a retry after a misleading timeout error)
    structurally impossible rather than just unlikely.
    """
    data = request.get_json()
    bucket_key = data.get("bucket_key")
    selected_ids = data.get("order_ids")  # optional - checkbox multi-select

    with state_lock:
        items = state["buckets"].get(bucket_key, [])
        if selected_ids:
            selected_set = set(selected_ids)
            items = [it for it in items if it.get("order_id") in selected_set]

    if not items:
        return jsonify({"error": "No orders found in that bucket"}), 400

    with jobs_lock:
        if bucket_key in active_ship_buckets:
            return jsonify({
                "error": f'A shipping job for "{bucket_key}" is already in progress. '
                         f'Please wait for it to finish - this prevents accidentally '
                         f'shipping the same orders twice.'
            }), 409
        active_ship_buckets.add(bucket_key)

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "running", "response": None}

    def run_job():
        try:
            results = ship_orders(items)
            combined_filename, included_orders, skipped_orders = build_combined_label_pdf(results, bucket_key)
            log_shipping_results(results, bucket_key, combined_filename)
            response = {
                "results": results,
                "combined_pdf_url": f"/api/labels/{combined_filename}" if combined_filename else None,
                "combined_pdf_page_count": len(included_orders),
                "combined_pdf_skipped": skipped_orders,
            }
            with jobs_lock:
                jobs[job_id] = {"status": "done", "response": response}
        except Exception as e:
            with jobs_lock:
                jobs[job_id] = {"status": "done", "response": {"error": str(e)}}
        finally:
            with jobs_lock:
                active_ship_buckets.discard(bucket_key)
            # Refresh right away so shipped orders drop out of the queue
            # immediately instead of waiting for the next scheduled poll.
            threading.Thread(target=poll_once, daemon=True).start()

    threading.Thread(target=run_job, daemon=True).start()

    return jsonify({"job_id": job_id, "status": "started"})


@app.route("/api/labels/<path:filename>")
def api_get_label(filename):
    return send_from_directory(LABELS_DIR, filename, as_attachment=False)


@app.route("/api/shipping_history")
def api_shipping_history():
    search = request.args.get("search", "")
    entries = get_shipping_history(search=search, limit=300)
    return jsonify({"entries": entries, "count": len(entries)})


@app.route("/api/reconcile", methods=["POST"])
def api_reconcile():
    """
    Starts a check of every order currently marked as failed in Shipping
    History against TikTok's real current status, fixing any that actually
    shipped despite our record showing a failure - see
    shipping.reconcile_failed_orders() for the full story on why this
    happens.

    Same async job pattern as /api/ship_bucket and for the same reason:
    checking many failed orders makes one real network call per order and
    can take a while, so this returns a job_id immediately rather than
    risking the same timeout/retry problem. Rejects with 409 if a
    reconciliation check is already running.
    """
    with jobs_lock:
        if reconcile_state["in_progress"]:
            return jsonify({"error": "A reconciliation check is already in progress. Please wait for it to finish."}), 409
        reconcile_state["in_progress"] = True

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "running", "response": None}

    def run_job():
        try:
            result = reconcile_failed_orders()
            response = {
                "checked": result["checked"],
                "fixed_count": len(result["fixed"]),
                "fixed_order_ids": result["fixed"],
                "still_unshipped": result["still_unshipped"],
                "not_found": result["not_found"],
                "combined_pdf_url": f"/api/labels/{result['combined_pdf_filename']}" if result["combined_pdf_filename"] else None,
            }
            with jobs_lock:
                jobs[job_id] = {"status": "done", "response": response}
        except Exception as e:
            with jobs_lock:
                jobs[job_id] = {"status": "done", "response": {"error": str(e)}}
        finally:
            with jobs_lock:
                reconcile_state["in_progress"] = False

    threading.Thread(target=run_job, daemon=True).start()

    return jsonify({"job_id": job_id, "status": "started"})


@app.route("/api/job_status/<job_id>")
def api_job_status(job_id):
    """Shared polling endpoint for both /api/ship_bucket and /api/reconcile
    jobs - always fast (just a dict lookup), so this request itself can
    never be the one that times out."""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job_id"}), 404
    return jsonify(job)


if __name__ == "__main__":
    load_state()
    poller = threading.Thread(target=poll_loop, daemon=True)
    poller.start()
    print(f"Dashboard running at http://localhost:{PORT}")
    print(f"Polling every {POLL_INTERVAL_SECONDS} seconds in the background.")
    app.run(host="0.0.0.0", port=PORT, debug=False)