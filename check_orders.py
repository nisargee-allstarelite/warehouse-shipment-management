import tiktok_api

ids = tiktok_api.get_all_order_ids(days_back=90, order_status="AWAITING_SHIPMENT")
print(f"\nTotal orders in Awaiting Shipment: {len(ids)}\n")

orders = tiktok_api.get_order_details(ids)
print(f"Sample of seller notes (first 20 of {len(orders)}):\n")
for o in orders[:20]:
    note = o.get("seller_note", "")
    print(f"Order {o.get('id')}: {note!r}")
