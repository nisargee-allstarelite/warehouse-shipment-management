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
from datetime import datetime

from flask import Flask, jsonify, request, render_template, send_from_directory, session, redirect, url_for
from dotenv import load_dotenv

import tiktok_api
from bucketing import bucket_orders
from shipping import ship_orders, build_combined_label_pdf, LABELS_DIR

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
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Polling TikTok for current Awaiting Shipment queue...")
    with state_lock:
        state["is_polling"] = True
        state["last_error"] = None

    try:
        orders = tiktok_api.fetch_awaiting_shipment_orders(days_back=90)
        buckets = bucket_orders(orders)
        buckets = apply_merges(buckets)

        with state_lock:
            state["buckets"] = buckets
            state["total_orders"] = len(orders)
            state["last_updated"] = datetime.now().isoformat()
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
    Purchases REAL shipping labels for every order currently in the given
    bucket, using the verified 3-step pipeline (Create Packages -> Batch
    Ship -> Get Document). This spends real money the moment it runs - the
    frontend is responsible for confirming with the person before calling
    this endpoint.
    """
    data = request.get_json()
    bucket_key = data.get("bucket_key")

    with state_lock:
        items = state["buckets"].get(bucket_key, [])
        order_ids = [item["order_id"] for item in items]

    if not order_ids:
        return jsonify({"error": "No orders found in that bucket"}), 400

    results = ship_orders(order_ids)

    combined_filename, included_orders, skipped_orders = build_combined_label_pdf(results, bucket_key)

    # Refresh right away so shipped orders drop out of the queue immediately
    # instead of waiting for the next scheduled poll.
    threading.Thread(target=poll_once, daemon=True).start()

    return jsonify({
        "results": results,
        "combined_pdf_url": f"/api/labels/{combined_filename}" if combined_filename else None,
        "combined_pdf_page_count": len(included_orders),
        "combined_pdf_skipped": skipped_orders,
    })


@app.route("/api/labels/<path:filename>")
def api_get_label(filename):
    return send_from_directory(LABELS_DIR, filename, as_attachment=False)


if __name__ == "__main__":
    load_state()
    poller = threading.Thread(target=poll_loop, daemon=True)
    poller.start()
    print(f"Dashboard running at http://localhost:{PORT}")
    print(f"Polling every {POLL_INTERVAL_SECONDS} seconds in the background.")
    app.run(host="0.0.0.0", port=PORT, debug=False)