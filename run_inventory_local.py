"""
Run ONLY the Pack & Deduct pages on your Mac for testing:
    python3 run_inventory_local.py   ->  http://localhost:5055/inventory

The TikTok poller is NOT started (so this never refreshes the TikTok token
or touches TikTok orders). Shopify changes are REAL -- same store and
Warehouse location as the server. Log is written to inventory_log.jsonl
in this folder.
"""
import os
from dotenv import load_dotenv

load_dotenv()
# The TikTok part is never called here -- fill any missing TikTok values with
# a dummy so the import doesn't stop (real values in .env are left untouched).
for k in ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_REFRESH_TOKEN", "TIKTOK_SHOP_CIPHER"):
    os.environ.setdefault(k, "unused-local-test")

import server  # noqa: E402

if __name__ == "__main__":
    print("Pack & Deduct (local test) at http://localhost:5055/inventory")
    server.app.run(host="127.0.0.1", port=5055, debug=False)
