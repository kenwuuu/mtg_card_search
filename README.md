# Setting up on a new server
1. git clone and cd into root dir
2. Set up venv so pip can install packages
```aiignore
# if setting up from scratch, run everything
# if already set up and just manually running a script, do not run python3 -m venv venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
3. Install Caddy. If you are setting this up, write this into `Caddyfile`.
```

```
4. Set up process manager for handling crashes and restarts by running `cat ` and then paste in the following
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
5. Run the server with the following commands. It will take about 20 seconds to come up since it 
indexes the `.ndjson` files first.
```
sudo systemctl daemon-reload
sudo systemctl enable mtg-card-search
sudo systemctl start mtg-card-search
```
6. Check logs with `journalctl -u mtg-card-search -f`

# Manually running data_updater
```aiignore
source venv/bin/activate
python3 data_updater.py
```
