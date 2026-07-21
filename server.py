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

from flask import Flask, jsonify, request, render_template
from dotenv import load_dotenv

import tiktok_api
from bucketing import bucket_orders

load_dotenv()

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", 300))
PORT = int(os.environ.get("PORT", 5000))
STATE_FILE = "state.json"

app = Flask(__name__)

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


if __name__ == "__main__":
    load_state()
    poller = threading.Thread(target=poll_loop, daemon=True)
    poller.start()
    print(f"Dashboard running at http://localhost:{PORT}")
    print(f"Polling every {POLL_INTERVAL_SECONDS} seconds in the background.")
    app.run(host="0.0.0.0", port=PORT, debug=False)
