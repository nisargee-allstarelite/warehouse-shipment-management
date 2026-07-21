# Warehouse Shipment Management

Live dashboard that polls TikTok Shop's Awaiting Shipment queue on a timer,
buckets orders by product/style (parsed from the seller note), and shows a
warehouse-manager-friendly view of what needs to be packed.

## Setup

```bash
# 1. Create a virtual environment (keeps dependencies isolated)
python3 -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set up your credentials
cp .env.example .env
# then open .env and fill in your real TikTok API values

# 4. Run it
python3 server.py
```

Open **http://localhost:5000** in your browser. Leave the terminal running -
that's what keeps the background poller alive.

## Project structure

```
.
├── server.py           Flask app + background poller + API routes
├── bucketing.py         Parses seller notes into product/style buckets
├── tiktok_api.py        TikTok Shop API auth, signing, and requests
├── templates/
│   └── index.html       Dashboard UI (auto-refreshing)
├── .env.example          Template for required environment variables
├── .env                  Your real credentials (gitignored, never commit)
├── requirements.txt
└── state.json            Auto-generated cache of current buckets (gitignored)
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `TIKTOK_APP_KEY` | yes | From TikTok Partner Center |
| `TIKTOK_APP_SECRET` | yes | From TikTok Partner Center |
| `TIKTOK_ACCESS_TOKEN` | yes | From the OAuth token exchange |
| `TIKTOK_SHOP_CIPHER` | yes | From Get Authorized Shops |
| `POLL_INTERVAL_SECONDS` | no (default 300) | How often to re-poll TikTok |
| `PORT` | no (default 5000) | Local server port |

## Notes / known limitations

- `TIKTOK_ACCESS_TOKEN` expires periodically and currently has to be
  refreshed manually via the OAuth flow - not yet automated with the
  refresh_token.
- Bucketing is exact-match on a parsed "base product name" - occasional
  near-duplicate buckets can appear from typos in seller notes (e.g. a
  misspelled product name). Use the merge button in the dashboard to fix
  these by hand; it's a safer default than trying to auto-merge similar
  text, which risks merging genuinely different products.
- Not yet implemented: automatically removing an order from its bucket
  once a shipping label is purchased for it (planned next).

## Deploying later

The app already reads `PORT` from the environment and binds to `0.0.0.0`,
so it's ready to deploy as-is to something like Render, Railway, or Fly.io.
Set the same environment variables from the table above in your hosting
platform's dashboard instead of a local `.env` file.
