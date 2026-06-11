# Setting up on a new server
1. git clone and cd into root dir
2. Install Caddy. If you are setting this  write this into `Caddyfile`.
```
```
3. Set up process manager for handling crashes and restarts by running `cat ` and then paste in the following
```
[Unit]
Description=MTG Card Search FastAPI Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/aura-api/mtg_card_search

ExecStart=/root/aura-api/mtg_card_search/.venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000

Restart=always
RestartSec=3

Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```
4. Run the server with the following commands. It will take about 20 seconds to come up since it 
indexes the `.ndjson` files first.
```
sudo systemctl daemon-reload
sudo systemctl enable mtg-card-search
sudo systemctl start mtg-card-search
```
5. Check logs with `journalctl -u mtg-card-search -f`