# MTG Card Search

A small FastAPI service for fast Magic: The Gathering card lookups. It indexes
Scryfall bulk-data NDJSON files by byte offset in memory, then serves single and
bulk card lookups by name, flavor/printed name, or set + collector number.
`data_updater.py` refreshes the underlying data from Scryfall on a cron.

Requires Python 3.12+.

For provisioning a new server, deploying updates, and the alerting wired
around `data_updater.py`, see [SETUP.md](SETUP.md).

## Running tests

`tests/test_data_updater.py` is the fast, no-network, no-real-data suite (lock,
sanity-check, alerting no-op paths) — run this one routinely:
```
.venv/bin/python3 -m pytest tests/
```

`tests/test_all_cards.py` walks every card in `CARD_JSON_DIR` and hits a
running server to check name/set lookups resolve — slow, exhaustive, not a
routine check (see [SETUP.md](SETUP.md#before-deploying-to-production) for
`scripts/smoke_test.sh`, the fast alternative). Start the server first, then:
```
python3 tests/test_all_cards.py
```
