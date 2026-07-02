# MTG Card Search

A small FastAPI service for fast Magic: The Gathering card lookups. It indexes
Scryfall bulk-data NDJSON files by byte offset in memory, then serves single and
bulk card lookups by name, flavor/printed name, or set + collector number.
`data_updater.py` refreshes the underlying data from Scryfall on a cron.

Requires Python 3.12+.

## Setting up on a new server

1. Clone the repo and `cd` into it.
   ```
   git clone <repo-url> mtg_card_search
   cd mtg_card_search
   ```

2. Create the venv and install dependencies.
   ```
   # Fresh setup: create the venv.
   # Already set up and just running a script manually? Skip this line and
   # just `source .venv/bin/activate`.
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Configure environment variables.
   ```
   cp .env.example .env
   ```
   Edit `.env` — see comments in `.env.example` for what each variable does
   (`CARD_JSON_DIR`, `BULK_DATA_TYPES`, `CORS_ORIGIN`, `DATASET_FALLBACKS`).
   `settings.py` fails fast with a clear error if anything required is missing.

4. Fetch the initial card data. The app refuses to start until the
   `.ndjson` files referenced by `BULK_DATA_TYPES` exist, so this must happen
   before the first server start.
   ```
   mkdir -p cards   # or wherever CARD_JSON_DIR points
   python3 data_updater.py
   ```
   This downloads Scryfall's bulk JSON and converts it to NDJSON; expect it
   to take roughly a minute depending on dataset size and network speed.

5. Install [Caddy](https://caddyserver.com/docs/install) as a reverse proxy
   in front of uvicorn. Example `Caddyfile` (adjust the domain, or use
   `:80`/`:443` with your own TLS setup):
   ```
   api.example.com {
       reverse_proxy localhost:8000
   }
   ```
   Then run `sudo caddy reload --config /etc/caddy/Caddyfile` (or restart the
   `caddy` service, depending on how it was installed).

6. Set up a process manager so the API survives crashes and reboots. Create
   `/etc/systemd/system/mtg-card-search.service`:
   ```ini
   [Unit]
   Description=MTG Card Search FastAPI Server
   After=network.target

   [Service]
   Type=simple
   # Prefer a dedicated non-root user over root where possible.
   User=root
   WorkingDirectory=/root/aura-api/mtg_card_search

   ExecStart=/root/aura-api/mtg_card_search/.venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000

   Restart=always
   RestartSec=3

   Environment=PYTHONUNBUFFERED=1

   [Install]
   WantedBy=multi-user.target
   ```
   Update `WorkingDirectory`/`ExecStart` to match the actual clone path.

7. Start the server. It takes ~20 seconds to come up on first boot since it
   indexes the `.ndjson` files before accepting traffic.
   ```
   sudo systemctl daemon-reload
   sudo systemctl enable mtg-card-search
   sudo systemctl start mtg-card-search
   ```

8. Verify it's up: `curl localhost:8000/v1/health` should return
   `{"status": "ok", "datasets": {...}}` with non-zero counts per dataset.
   Check logs with `journalctl -u mtg-card-search -f`.

9. Set up a cron job to keep card data fresh. Run `crontab -e` and add:
   ```
   TZ=America/New_York
   0 5 * * 2 /root/aura-api/mtg_card_search/.venv/bin/python3 /root/aura-api/mtg_card_search/data_updater.py >> /var/log/mtg-card-search-updater.log 2>&1
   ```
   `api.py` watches the `.ndjson` files and rebuilds the affected in-memory
   index automatically when `data_updater.py` overwrites them — no restart
   needed after a data refresh.

## Deploying an update to an existing server

```
cd /root/aura-api/mtg_card_search
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart mtg-card-search
```
Then re-check `curl localhost:8000/v1/health`. If something's wrong,
`git checkout <previous-commit>` and restart again to roll back.

## Manually running data_updater

```
source .venv/bin/activate
python3 data_updater.py
```

## Running tests

`tests/test_all_cards.py` walks every card in `CARD_JSON_DIR` and hits a
running server to check name/set lookups resolve. Start the server first,
then:
```
python3 tests/test_all_cards.py
```
