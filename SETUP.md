# Server setup & deployment

This covers provisioning a server to run `mtg_card_search` in production,
deploying updates to it, and the alerting wired around `data_updater.py`. See
[README.md](README.md) for what the project is and how to develop on it.

## Setting up on a new server

`scripts/setup.sh` automates steps 1-9 below (venv, deps, `.env` bootstrap,
initial data fetch, systemd unit, cron) — run `./scripts/setup.sh --help`-style
usage comments at the top of the file for flags, or read the manual steps here
if you'd rather do it by hand or understand what it's doing. Either way, step
5 (Caddy) is never automated — it's domain/TLS-specific.

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

   Already have a working `.env` locally (with alerting keys filled in) and
   just want it on the server as-is? Skip editing by hand and push it over
   scp instead, from your local machine:
   ```
   scripts/push_env.sh -h user@your-server -d /root/aura-api/mtg_card_search
   ```
   It prompts before overwriting an existing remote `.env` and chmods the
   copy to `600` afterward. Run `scripts/push_env.sh` with no args for the
   full flag list (`-i` for an identity file, `-p` for a non-default SSH
   port).

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

   Keep Caddy on the **same host** as uvicorn unless you know what you're
   doing — the API's per-IP rate limiting depends on it. See
   [Rate limiting behind the proxy](#rate-limiting-behind-the-proxy) before
   putting a proxy/LB on a different host or exposing port 8000 directly.

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

## Before deploying to production

There's no staging environment for this project, so confidence comes from
layering fast, cheap checks before anything touches the real server:

1. `.venv/bin/python3 -m pytest tests/` — the fast suite (`test_data_updater.py`)
   exercises the run lock, the sanity-check accept/reject paths, the
   healthchecks.io ping sequence, and the PostHog/Sentry no-op behavior, all
   against `tmp_path` fixtures — no network, no real data, runs in ~2 seconds.
   This is what to run on every commit, not just before a deploy.
2. `.venv/bin/python3 -c "import api; import data_updater"` — catches
   config/import errors (a broken `.env`, a typo) before they reach the server.
3. Run the server locally against real data and `./scripts/smoke_test.sh` it —
   covers `/v1/health`, a known-good lookup, a 404, and a mixed bulk lookup in
   a few seconds. Run this again immediately after every deploy, against the
   real server, not just locally beforehand.
4. For a genuinely new environment (new server, first deploy), run
   `scripts/setup.sh` against a throwaway VM or container first — cheap
   insurance that the automated bootstrap actually works before trusting it
   against production.
5. For exhaustive (but slow) coverage, `tests/test_all_cards.py` walks every
   card in the dataset against a running server — useful after a Scryfall
   schema change or when debugging a specific lookup class, not as a routine
   pre-deploy gate.

None of this replaces the runtime safety nets already built in — the atomic
`.ndjson` swap and entry-count sanity check mean a bad `data_updater.py` run
can't corrupt what's being served, and `/v1/health`'s `stale` flag plus the
alerting in the section below catch problems that show up only after
deploying.

## Deploying an update to an existing server

```
cd /root/aura-api/mtg_card_search
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart mtg-card-search
```
Then run `./scripts/smoke_test.sh` (or at minimum
`curl localhost:8000/v1/health`). If something's wrong,
`git checkout <previous-commit>` and restart again to roll back.

## Manually running data_updater

```
source .venv/bin/activate
python3 data_updater.py
```

## Rate limiting behind the proxy

`api.py` rate-limits per client IP via slowapi, keyed by `get_remote_address`
(which reads `request.client.host`): `/v1/cards/{id}` at 200/s, and the
multi-card `/v1/cards/bulk/lookup` and `/v1/cards/random` at 2/s. Limits are
per source IP **and** per route, so one noisy client can't throttle everyone
else — *provided "client IP" is actually the end user's IP.*

Behind Caddy it isn't, directly: the socket uvicorn sees belongs to Caddy, not
the user. What saves us is uvicorn's proxy-headers middleware, which rewrites
the client to the real IP from the `X-Forwarded-For` header Caddy sets — but
**only** when the immediate peer is in `--forwarded-allow-ips`. That list
defaults to `127.0.0.1` (override with the `FORWARDED_ALLOW_IPS` env var), and
`proxy_headers` is on by default, so the setup documented above — Caddy on the
**same host** as uvicorn — works out of the box with no extra flags: per-IP
limiting sees real client IPs.

Where this silently breaks, and what to do:

- **Proxy on a different host.** If Caddy (or an upstream LB/CDN) runs anywhere
  other than `127.0.0.1`, its peer IP isn't trusted, uvicorn ignores
  `X-Forwarded-For`, and *every* request collapses to the proxy's single IP —
  the limit becomes effectively **global** and a handful of clients can 429
  everyone. Fix: add `--forwarded-allow-ips=<proxy-ip>` to the uvicorn
  `ExecStart` in the systemd unit (or set `FORWARDED_ALLOW_IPS`).
- **Direct access to port 8000.** The systemd `ExecStart` binds `0.0.0.0:8000`,
  so if that port is reachable from outside it bypasses Caddy (and TLS)
  entirely. Firewall it so all traffic goes through Caddy and carries a trusted
  `X-Forwarded-For`. (uvicorn only trusts a request with exactly one
  `X-Forwarded-For` header — which Caddy's `reverse_proxy` sets — so a client
  appending its own spoofed value is ignored.)
- **Multiple uvicorn workers.** slowapi's counters here are in-memory and
  per-process. The current `ExecStart` runs a single worker, so this is fine
  today — but adding `--workers N` gives each worker its own counters, making
  the effective limit `N ×` the configured rate. To keep one shared limit
  across workers, configure a slowapi storage backend (e.g. Redis) instead of
  the in-memory default.

## Alerting

`data_updater.py` runs unattended via cron, so failures need to surface on
their own. Two mechanisms are wired (set the env var to activate — see
`.env.example`), one is deliberately left unconfigured:

- **healthchecks.io** (`HEALTHCHECK_PING_URL`) — a dead-man's-switch. The
  script pings `<url>/start` before a run, `<url>` on success, `<url>/fail`
  immediately on failure. healthchecks.io alerts you if it *doesn't* hear a
  success/fail ping within the schedule + grace period you configure — this
  is the only mechanism here that catches the job not running at all (box
  down, cron never re-armed after a reboot, etc.), since nothing running on a
  dead machine can page you about itself.
  To connect it: create a free account and a check at
  [healthchecks.io](https://healthchecks.io), set its schedule to match the
  cron cadence (weekly, with a grace period of a few hours), copy the ping
  URL it gives you (`https://hc-ping.com/<uuid>`), and set
  `HEALTHCHECK_PING_URL` to that URL in `.env`. Nothing else to do — the
  `/start`, success, and `/fail` pings are already wired in.
- **PostHog** (`POSTHOG_API_KEY`) — captures `data_update_succeeded` (with
  per-dataset entry counts and run duration) and `data_update_failed` (with
  the error) as events. Mainly useful as a trend: card counts jump
  predictably around new set releases, so a dashboard of this over time
  doubles as a release-tracking signal, not just a health check.
- **Sentry** (`SENTRY_DSN`) — captures the exception on a failed run with a
  full stack trace. Left plumbed but inactive: `sentry-sdk` is intentionally
  not in `requirements.txt` yet, so this needs both `pip install sentry-sdk`
  and `SENTRY_DSN` set to activate. No further code changes needed either way
  — `data_updater.py` already calls `capture_exception()` in its failure path.

### Why not just cron + systemd alone?

Converting the cron job to a **systemd timer** (a `.timer` unit triggering a
`.service` unit, like cron but integrated with systemd) buys two things for
free: `journalctl` captures all output automatically (today's crontab line
redirects to a plain file that nothing rotates or watches), and a
`.service`'s `OnFailure=` directive can trigger another unit — e.g. one that
posts to a Slack webhook — automatically whenever the run exits non-zero, no
code changes needed.

The gap: `OnFailure=` runs on the *same machine* as the failed job. If the
box crashes, loses power, or cron/systemd itself doesn't come back after a
reboot, there's nothing left on that machine to fire the failure hook — this
is exactly the "job silently stopped running" failure mode, and only an
external watchdog (healthchecks.io) can catch it, because it lives outside
the machine that might be dead. `/v1/health`'s `stale` flag (10-day
threshold) is a second, independent backstop for the same gap if you'd rather
poll the API than run a systemd timer.

Recommendation: a systemd timer + `OnFailure=` is worth doing regardless (better
logs, zero-cost local failure alerting), but keep healthchecks.io too — it's
the only piece that covers "the whole job stopped running."
