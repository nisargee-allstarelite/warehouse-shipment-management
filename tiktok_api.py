"""
Shared TikTok Shop API functions - signing, requests, order fetching.
Credentials are loaded from a .env file (see .env.example) - never hardcoded here.
"""

import hashlib
import hmac
import json
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

APP_KEY = os.environ.get("TIKTOK_APP_KEY")
APP_SECRET = os.environ.get("TIKTOK_APP_SECRET")
ACCESS_TOKEN = os.environ.get("TIKTOK_ACCESS_TOKEN")
SHOP_CIPHER = os.environ.get("TIKTOK_SHOP_CIPHER")

missing = [name for name, val in [
    ("TIKTOK_APP_KEY", APP_KEY),
    ("TIKTOK_APP_SECRET", APP_SECRET),
    ("TIKTOK_ACCESS_TOKEN", ACCESS_TOKEN),
    ("TIKTOK_SHOP_CIPHER", SHOP_CIPHER),
] if not val]
if missing:
    raise RuntimeError(
        f"Missing required environment variables: {', '.join(missing)}. "
        f"Copy .env.example to .env and fill in your values."
    )

BASE_URL = "https://open-api.tiktokglobalshop.com"
VERSION = "202309"


def sign_request(path, params, secret, body=None):
    sorted_keys = sorted(k for k in params if k not in ("sign", "access_token"))
    base_string = path
    for k in sorted_keys:
        base_string += f"{k}{params[k]}"
    if body is not None:
        base_string += json.dumps(body, separators=(",", ":"))
    base_string = f"{secret}{base_string}{secret}"
    return hmac.new(secret.encode(), base_string.encode(), hashlib.sha256).hexdigest()


def make_request(method, path, query_params=None, body=None, quiet=False):
    query_params = query_params or {}
    params = {
        "app_key": APP_KEY,
        "shop_cipher": SHOP_CIPHER,
        "timestamp": int(time.time()),
        **query_params,
    }
    params["sign"] = sign_request(path, params, APP_SECRET, body=body)

    headers = {
        "x-tts-access-token": ACCESS_TOKEN,
        "content-type": "application/json",
    }

    url = f"{BASE_URL}{path}"
    if method == "GET":
        resp = requests.get(url, params=params, headers=headers)
    else:
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else None
        resp = requests.post(url, params=params, headers=headers, data=body_str)

    data = resp.json()
    if data.get("code") != 0 and not quiet:
        print(f"WARNING: API error on {path}: {data}")
    return data


def get_all_order_ids(days_back=90, order_status=None):
    """Paginate through Get Order List to collect every order ID matching the filter."""
    path = f"/order/{VERSION}/orders/search"
    create_time_ge = int(time.time()) - (days_back * 86400)

    order_ids = []
    page_token = None

    while True:
        query = {"page_size": 50}
        if page_token:
            query["page_token"] = page_token

        body = {"create_time_ge": create_time_ge}
        if order_status:
            body["order_status"] = order_status

        data = make_request("POST", path, query_params=query, body=body)
        if data.get("code") != 0:
            break
        orders = data.get("data", {}).get("orders", [])
        order_ids.extend(o["id"] for o in orders)

        page_token = data.get("data", {}).get("next_page_token")
        if not page_token or not orders:
            break

    return order_ids


def get_order_details(order_ids):
    path = f"/order/{VERSION}/orders"
    all_orders = []
    for i in range(0, len(order_ids), 50):
        batch = order_ids[i:i + 50]
        data = make_request("GET", path, query_params={"ids": ",".join(batch)}, quiet=True)
        if data.get("code") != 0:
            print(f"WARNING: skipping a batch due to API error: {data.get('message')}")
            continue
        orders = (data.get("data") or {}).get("orders", [])
        all_orders.extend(orders)
    return all_orders


def fetch_awaiting_shipment_orders(days_back=90):
    """One-call convenience: full current Awaiting Shipment queue with details."""
    ids = get_all_order_ids(days_back=days_back, order_status="AWAITING_SHIPMENT")
    if not ids:
        return []
    return get_order_details(ids)
