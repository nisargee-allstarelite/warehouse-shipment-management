"""
shopify_inventory.py -- All Star Elite Shopify, Warehouse location only.

Used by the "Pack & Deduct" page: scan a product while packing TikTok
orders -> preview -> confirm -> the qty is subtracted from the ASE
Warehouse inventory in Shopify.

Same Shopify mechanics as store-refill / restock-cadence (refill_push.py +
shopify_client.py), copied so this app has no dependency on those repos:
  - Auth: client-credentials token exchange (SHOPIFY_CLIENT_ID /
    SHOPIFY_CLIENT_SECRET in .env -- same app as store-refill), token
    cached and refreshed before its ~24h expiry.
  - Reads the LIVE 'available' qty right before writing.
  - inventoryAdjustQuantities with changeFromQuantity (Shopify rejects the
    write if the number moved since we read it) + @idempotent key.
"""
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

API_VERSION = "2026-07"
SHOP_DOMAIN = os.environ.get("ASE_SHOP_DOMAIN", "allstarelite-com.myshopify.com")
SHOP_HANDLE = os.environ.get("ASE_SHOP_HANDLE", "allstarelite-com")
WAREHOUSE_LOCATION_ID = os.environ.get("ASE_WAREHOUSE_LOCATION_ID", "109015204133")


def history_url(inventory_item_id):
    return (f"https://admin.shopify.com/store/{SHOP_HANDLE}/products/inventory/"
            f"{int(inventory_item_id)}/inventory_history?location_id={WAREHOUSE_LOCATION_ID}")


def edit_url(product_id, variant_id):
    return (f"https://admin.shopify.com/store/{SHOP_HANDLE}/products/"
            f"{int(product_id)}/variants/{int(variant_id)}?fromInventory=true")


def _num(gid):
    return gid.split("/")[-1] if gid else None


class ShopifyInventory:
    def __init__(self):
        self.client_id = os.environ.get("SHOPIFY_CLIENT_ID")
        self.client_secret = os.environ.get("SHOPIFY_CLIENT_SECRET")
        self.base = f"https://{SHOP_DOMAIN}/admin/api/{API_VERSION}/graphql.json"
        self._token, self._expires = None, datetime.min
        self._lock = threading.Lock()
        self.session = requests.Session()

    def configured(self):
        return bool(self.client_id and self.client_secret)

    def _token_value(self, force=False):
        with self._lock:
            if force or not self._token or self._expires < datetime.utcnow() + timedelta(minutes=5):
                if not self.configured():
                    raise RuntimeError("SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET are not set in .env")
                r = requests.post(f"https://{SHOP_DOMAIN}/admin/oauth/access_token", timeout=20, json={
                    "client_id": self.client_id, "client_secret": self.client_secret,
                    "grant_type": "client_credentials"})
                r.raise_for_status()
                d = r.json()
                self._token = d["access_token"]
                self._expires = datetime.utcnow() + timedelta(seconds=int(d.get("expires_in", 86400)))
            return self._token

    def graphql(self, query, variables=None):
        body = {"query": query, "variables": variables or {}}
        force = False
        for _ in range(5):
            headers = {"X-Shopify-Access-Token": self._token_value(force), "Content-Type": "application/json"}
            r = self.session.post(self.base, json=body, headers=headers, timeout=30)
            if r.status_code == 401 and not force:
                force = True
                continue
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 2)))
                continue
            r.raise_for_status()
            d = r.json()
            if d.get("errors"):
                msg = str(d["errors"])
                if "THROTTLED" in msg:
                    time.sleep(2)
                    continue
                raise RuntimeError(f"Shopify error: {msg[:300]}")
            return d["data"]
        raise RuntimeError("Shopify kept rate-limiting -- try again in a minute")

    # ------------------------------------------------------------ search
    _SEARCH = """
    query($q: String!, $loc: ID!) {
      productVariants(first: 25, query: $q) {
        nodes {
          id sku barcode title
          selectedOptions { name value }
          product { id title status }
          inventoryItem {
            id tracked
            inventoryLevel(locationId: $loc) { quantities(names: ["available"]) { quantity } }
          }
        }
      }
    }"""

    def _shape(self, n):
        lvl = n["inventoryItem"]["inventoryLevel"]
        opts = {o["name"]: o["value"] for o in n.get("selectedOptions") or []}
        return {
            "variant_id": _num(n["id"]), "product_id": _num(n["product"]["id"]),
            "inventory_item_id": _num(n["inventoryItem"]["id"]),
            "style": n["product"]["title"], "variant": n["title"],
            "size": opts.get("Size") or n["title"], "sku": n.get("sku") or "",
            "barcode": n.get("barcode") or "", "status": n["product"]["status"],
            "tracked": n["inventoryItem"].get("tracked"),
            "warehouse_qty": lvl["quantities"][0]["quantity"] if lvl else None,
            "history": history_url(_num(n["inventoryItem"]["id"])),
            "edit": edit_url(_num(n["product"]["id"]), _num(n["id"])),
        }

    def find(self, term):
        """Scanner input first (exact barcode, then exact SKU), then a
        general text search so a typed style name also works."""
        term = (term or "").strip()
        if not term:
            return {"match": "none", "results": []}
        loc = f"gid://shopify/Location/{WAREHOUSE_LOCATION_ID}"
        safe = term.replace("\\", "\\\\").replace('"', '\\"')
        exact = self.graphql(self._SEARCH, {"q": f'barcode:"{safe}" OR sku:"{safe}"', "loc": loc})
        rows = [self._shape(n) for n in exact["productVariants"]["nodes"]]
        t = term.lower()
        by_barcode = [r for r in rows if r["barcode"].lower() == t]
        by_sku = [r for r in rows if r["sku"].lower() == t]
        if by_barcode:
            return {"match": "barcode", "results": by_barcode}
        if by_sku:
            return {"match": "sku", "results": by_sku}
        loose = self.graphql(self._SEARCH, {"q": safe, "loc": loc})
        return {"match": "search", "results": [self._shape(n) for n in loose["productVariants"]["nodes"]]}

    # ------------------------------------------------------------ inventory
    def get_qty(self, inventory_item_id):
        d = self.graphql("""
        query($id: ID!, $loc: ID!) {
          inventoryItem(id: $id) { inventoryLevel(locationId: $loc) { quantities(names: ["available"]) { quantity } } }
        }""", {"id": f"gid://shopify/InventoryItem/{inventory_item_id}",
               "loc": f"gid://shopify/Location/{WAREHOUSE_LOCATION_ID}"})
        lvl = (d.get("inventoryItem") or {}).get("inventoryLevel")
        return lvl["quantities"][0]["quantity"] if lvl else None

    def adjust(self, inventory_item_id, delta, change_from_quantity):
        d = self.graphql("""
        mutation inventoryAdjustQuantities($input: InventoryAdjustQuantitiesInput!, $key: String!) {
          inventoryAdjustQuantities(input: $input) @idempotent(key: $key) {
            userErrors { field message }
            inventoryAdjustmentGroup { createdAt }
          }
        }""", {"input": {"reason": "correction", "name": "available", "changes": [{
            "delta": delta,
            "inventoryItemId": f"gid://shopify/InventoryItem/{inventory_item_id}",
            "locationId": f"gid://shopify/Location/{WAREHOUSE_LOCATION_ID}",
            "changeFromQuantity": change_from_quantity}]},
            "key": str(uuid.uuid4())})
        errs = d["inventoryAdjustQuantities"]["userErrors"]
        if errs:
            raise RuntimeError(f"Shopify rejected the change: {errs}")


_client = None


def get_client():
    """Replaced in tests with a fake."""
    global _client
    if _client is None:
        _client = ShopifyInventory()
    return _client
