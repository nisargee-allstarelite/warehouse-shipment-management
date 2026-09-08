"""
Shared TikTok Shop API functions - signing, requests, order fetching.
Credentials are loaded from a .env file (see .env.example) - never hardcoded here.

Access tokens are refreshed automatically:
  - Proactively, a day before the known expiry, so we never actually hit
    a live "expired credentials" error in normal operation.
  - Reactively, as a safety net: if a request ever comes back with the
    expired-credentials error anyway (e.g. right after a restart before
    we know the real expiry timestamp), it refreshes once and retries
    the exact same request automatically.

TikTok rotates the refresh_token every time it's used, so every refresh
saves BOTH the new access_token and the new refresh_token - reusing an old
rotated-out refresh_token would fail the next refresh attempt.
"""

import hashlib
import hmac
import json
import os
import threading
import time
import requests
from dotenv import load_dotenv

load_dotenv()

ENV_PATH = ".env"

APP_KEY = os.environ.get("TIKTOK_APP_KEY")
APP_SECRET = os.environ.get("TIKTOK_APP_SECRET")
ACCESS_TOKEN = os.environ.get("TIKTOK_ACCESS_TOKEN")
REFRESH_TOKEN = os.environ.get("TIKTOK_REFRESH_TOKEN")
SHOP_CIPHER = os.environ.get("TIKTOK_SHOP_CIPHER")

# Unix timestamp the current access token expires at (TikTok returns this
# as an absolute timestamp, not a duration). 0 means "unknown yet" - in
# that case we just rely on the reactive refresh-on-error path below
# instead of blocking startup on an extra API call.
try:
    ACCESS_TOKEN_EXPIRE_AT = int(os.environ.get("TIKTOK_ACCESS_TOKEN_EXPIRE_AT", "0") or 0)
except ValueError:
    ACCESS_TOKEN_EXPIRE_AT = 0

missing = [name for name, val in [
    ("TIKTOK_APP_KEY", APP_KEY),
    ("TIKTOK_APP_SECRET", APP_SECRET),
    ("TIKTOK_ACCESS_TOKEN", ACCESS_TOKEN),
    ("TIKTOK_REFRESH_TOKEN", REFRESH_TOKEN),
    ("TIKTOK_SHOP_CIPHER", SHOP_CIPHER),
] if not val]
if missing:
    raise RuntimeError(
        f"Missing required environment variables: {', '.join(missing)}. "
        f"Copy .env.example to .env and fill in your values."
    )

BASE_URL = "https://open-api.tiktokglobalshop.com"
REFRESH_URL = "https://auth.tiktok-shops.com/api/v2/token/refresh"
VERSION = "202309"

# Refresh this long before actual expiry - gives comfortable buffer for
# clock drift, a slow request, or the daily check just missing the window.
REFRESH_MARGIN_SECONDS = 24 * 60 * 60  # 1 day

_refresh_lock = threading.Lock()

# If a refresh attempt ever fails (e.g. the refresh_token itself expired -
# it lasts about a year), this holds the reason so server.py can surface
# it visibly on the dashboard instead of it only ever showing up in
# server.log where nobody's watching.
LAST_REFRESH_ERROR = None


def _update_env_file(updates):
    """Atomically rewrite specific KEY=value lines in .env, leaving every
    other line untouched. Writes to a temp file and renames it into place,
    so a crash mid-write can never leave a half-written .env behind."""
    with open(ENV_PATH) as f:
        lines = f.readlines()

    seen = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        matched = False
        for key, value in updates.items():
            if stripped.startswith(f"{key}="):
                new_lines.append(f"{key}={value}\n")
                seen.add(key)
                matched = True
                break
        if not matched:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}\n")

    tmp_path = ENV_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        f.writelines(new_lines)
    os.replace(tmp_path, ENV_PATH)


def refresh_access_token():
    """Exchanges the current refresh_token for a new access_token + a new
    refresh_token, updates the in-memory globals this module uses
    immediately (no restart needed), and persists both to .env so they
    survive one. Thread-safe - concurrent callers just wait on the lock
    for the one in-flight refresh rather than firing duplicate requests."""
    global ACCESS_TOKEN, REFRESH_TOKEN, ACCESS_TOKEN_EXPIRE_AT, LAST_REFRESH_ERROR

    with _refresh_lock:
        resp = requests.get(
            REFRESH_URL,
            params={
                "app_key": APP_KEY,
                "app_secret": APP_SECRET,
                "refresh_token": REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
        )
        data = resp.json()

        if data.get("code") != 0:
            LAST_REFRESH_ERROR = data.get("message", "Unknown error refreshing token")
            print(f"ERROR: token refresh failed: {data}")
            return False

        token_data = data["data"]
        ACCESS_TOKEN = token_data["access_token"]
        REFRESH_TOKEN = token_data["refresh_token"]
        ACCESS_TOKEN_EXPIRE_AT = token_data.get("access_token_expire_in", 0)
        LAST_REFRESH_ERROR = None

        _update_env_file({
            "TIKTOK_ACCESS_TOKEN": ACCESS_TOKEN,
            "TIKTOK_REFRESH_TOKEN": REFRESH_TOKEN,
            "TIKTOK_ACCESS_TOKEN_EXPIRE_AT": ACCESS_TOKEN_EXPIRE_AT,
        })

        print("Token refreshed successfully - new access_token and refresh_token saved to .env.")
        return True


def _ensure_fresh_token():
    """Cheap check (just a timestamp comparison) called before every
    request. Only actually calls the refresh endpoint when we're within
    the margin of real expiry, so this costs nothing on every other call."""
    if ACCESS_TOKEN_EXPIRE_AT and time.time() > (ACCESS_TOKEN_EXPIRE_AT - REFRESH_MARGIN_SECONDS):
        refresh_access_token()


def sign_request(path, params, secret, body=None):
    sorted_keys = sorted(k for k in params if k not in ("sign", "access_token"))
    base_string = path
    for k in sorted_keys:
        base_string += f"{k}{params[k]}"
    if body is not None:
        base_string += json.dumps(body, separators=(",", ":"))
    base_string = f"{secret}{base_string}{secret}"
    return hmac.new(secret.encode(), base_string.encode(), hashlib.sha256).hexdigest()


def make_request(method, path, query_params=None, body=None, quiet=False, _is_retry=False):
    _ensure_fresh_token()

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

    # Reactive safety net: if the proactive check above still missed it
    # (e.g. right after a restart, before we know the real expiry), catch
    # TikTok's specific expired-credentials error here, refresh once, and
    # retry this exact request a single time before giving up for real.
    if data.get("code") == 105002 and not _is_retry:
        print("Access token expired mid-request - refreshing and retrying once.")
        if refresh_access_token():
            return make_request(method, path, query_params=query_params, body=body, quiet=quiet, _is_retry=True)

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
