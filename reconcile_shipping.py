"""
reconcile_shipping.py - command-line wrapper around
shipping.reconcile_failed_orders(). Useful for SSH/cron use; the same
logic is also available as a button on the Shipping History page
(server.py's /api/reconcile) - both call the exact same function, so
there's only one implementation of this logic to trust.

Finds every order whose most recent Shipping History entry shows failure,
checks TikTok's real current status for each, and fixes any that actually
shipped despite our record showing a failure. See the docstring on
shipping.reconcile_failed_orders() for the full story on why this happens
(TikTok's 50-package batch-ship limit).

Safe to re-run any time - it only ever looks at orders whose latest record
is still a failure, so already-reconciled orders are automatically skipped
on the next run.

Run with: python3 reconcile_shipping.py
"""

import shipping


def main():
    print("Checking for orders marked failed that may have actually shipped...")
    result = shipping.reconcile_failed_orders()

    print(f"Found {result['checked']} order(s) currently marked as failed.")
    if result["checked"] == 0:
        print("Nothing to reconcile.")
        return

    print(f"\n{len(result['fixed'])} order(s) actually shipped on TikTok's side - records fixed.")
    print(f"{len(result['still_unshipped'])} order(s) confirmed still NOT shipped - these need real action.")
    if result["not_found"]:
        print(f"{len(result['not_found'])} order(s) could not be found at all "
              f"(deleted/invalid?) - check manually: {result['not_found']}")

    if result["combined_pdf_filename"]:
        print(f"\nCombined reprintable label PDF saved: labels/{result['combined_pdf_filename']}")

    if result["still_unshipped"]:
        print("\n" + "=" * 60)
        print("These orders are CONFIRMED still NOT shipped - they genuinely")
        print("need to be shipped for real:")
        print("=" * 60)
        for oid in result["still_unshipped"]:
            print(f"  {oid}")


if __name__ == "__main__":
    main()