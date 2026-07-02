"""
This file is responsible for pulling card data and converting it into NDJSON
and then generating a 'name -> row number' index file to enable fast lookups
of the NDJSON
We pull card data from Scryfall by downloading their bulk card JSON file
This file is stateless and does not manage its own update frequency, use a
cron scheduler for scheduling tasks. I think that updating every 5 or 7 days
should be relatively safe. MTG releases full data on new sets about 3 weeks
before physical release. Worst case, we'd get the cards updated ~2 weeks
before physical release.
"""
import fcntl
import json
import logging
import os
import sys
import tracemalloc
from decimal import Decimal
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import perf_counter

import ijson
import posthog
import requests

from settings import settings

BULK_DATA_TYPES = settings.dataset_names
CHUNK_SIZE = 20 * 1024 * 1024  # 20 MB
FOLDER = str(settings.card_json_dir)
HEADERS = {
    "User-Agent": "Aura0/1.0 (kenqiwu@gmail.com)",  # Scryfall asks for app name + contact
    "Accept": "*/*"
}

# Prevents a run from silently overlapping a previous one still in progress
# (e.g. cron firing again before a slow download finishes). Held for the
# lifetime of the process; released automatically on exit, even a crash.
LOCK_PATH = Path(FOLDER) / ".data_updater.lock"

# Per-dataset entry counts from the last run that passed the sanity check
# below, so a new run can tell "smaller than before" without re-scanning the
# (large) previous .ndjson file.
COUNTS_PATH = Path(FOLDER) / ".dataset_counts.json"

# DESIGN DECISION: a new dataset file is rejected if it has fewer than this
# fraction of the previous known-good entry count. Scryfall's card count only
# grows release over release, so a sharp drop almost always means a truncated
# download or a Scryfall-side data problem, not a legitimate dataset shrink.
MIN_COUNT_RATIO = 0.9

LOG_PATH = Path(__file__).parent / "data_updater.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


class DataSanityError(RuntimeError):
    """Raised when a freshly downloaded dataset looks corrupt/truncated."""


def time_it(title):
    def decorator(func):
        @wraps(func)  # Keeps the original function's metadata
        def wrapper(*args, **kwargs):
            start = perf_counter()
            result = func(*args, **kwargs) # Run the actual function
            end = perf_counter()
            logger.info(f"{title} took {end - start:.6f} seconds")
            return result                  # Return the function's result
        return wrapper
    return decorator


def acquire_lock():
    """Return an open, exclusively-locked file handle, or None if another run holds it."""
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None
    return lock_file


def _load_counts() -> dict:
    if COUNTS_PATH.exists():
        return json.loads(COUNTS_PATH.read_text())
    return {}


def _save_counts(counts: dict) -> None:
    COUNTS_PATH.write_text(json.dumps(counts))


# ---------------------------------------------------------------------------
# Alerting — see README's "Alerting" section for the full writeup.
# PostHog and healthchecks.io are wired (set the env var to activate); Sentry
# is intentionally left plumbed but inactive pending a decision on whether to
# add it as a dependency.
# ---------------------------------------------------------------------------

def init_posthog() -> bool:
    """Configure the posthog client. Returns False (no-op) if unconfigured."""
    if not settings.posthog_api_key:
        logger.info("POSTHOG_API_KEY not set; skipping PostHog event tracking.")
        return False
    posthog.api_key = settings.posthog_api_key
    posthog.host = settings.posthog_host
    return True


def notify_posthog(enabled: bool, event: str, properties: dict) -> None:
    if not enabled:
        return
    try:
        posthog.capture(distinct_id="data_updater", event=event, properties=properties)
        # This is a short-lived script, not a long-running server — flush
        # synchronously so the event ships before the process exits, rather
        # than relying on posthog's background batching thread.
        posthog.shutdown()
    except Exception:
        logger.exception(f"Failed to send PostHog event {event!r}")


def ping_healthcheck(suffix: str = "") -> None:
    """Ping the configured dead-man's-switch. No-op unless HEALTHCHECK_PING_URL
    is set. `suffix` is '' on success, '/start' before the run, '/fail' on
    failure — the healthchecks.io ping-URL convention."""
    if not settings.healthcheck_ping_url:
        return
    try:
        requests.get(f"{settings.healthcheck_ping_url}{suffix}", timeout=10)
    except requests.RequestException:
        logger.warning(f"Failed to ping healthcheck URL (suffix={suffix!r})", exc_info=True)


def init_sentry():
    """PLUMBED BUT NOT WIRED: sentry-sdk is not in requirements.txt yet, and
    this no-ops without SENTRY_DSN set. `pip install sentry-sdk`, set
    SENTRY_DSN in .env, and this activates with no further code changes."""
    if not settings.sentry_dsn:
        return None
    try:
        import sentry_sdk
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed; skipping. `pip install sentry-sdk`.")
        return None
    sentry_sdk.init(dsn=settings.sentry_dsn)
    return sentry_sdk


@time_it(title="Downloading new cards")
def download_bulk_data():
    """
    Downloads Scryfall bulk cards to `cards.json`.
    This generally takes ~12 seconds to run; memory usage <5MB.
    :return: None
    """
    def get_bulk_data_items() -> dict:
        response = requests.get('https://api.scryfall.com/bulk-data', headers=HEADERS)
        response.raise_for_status()
        return response.json()

    def get_bulk_download_urls(bulk_data, bulk_data_type):
        for file in bulk_data['data']:
            if file['type'] == bulk_data_type:
                return file['download_uri']

        raise ValueError(
            f"No bulk data type '{bulk_data_type}' found. "
            f"Available types: {[f['type'] for f in bulk_data['data']]}"
        )

    bulk_data = get_bulk_data_items()
    urls = {data_type: '' for data_type in BULK_DATA_TYPES}
    for bulk_data_type in BULK_DATA_TYPES:
        urls[bulk_data_type] = (get_bulk_download_urls(bulk_data, bulk_data_type))

    for data_type, url in urls.items():
        with requests.get(url, stream=True) as response:
            response.raise_for_status()

            with open(f'{FOLDER}/{data_type}.json', 'wb') as f:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:  # filter out keep-alive chunks
                        f.write(chunk)
                        f.flush()

@time_it(title="Converting JSON to NDJSON")
def convert_json_to_ndjson():
    """
    Converts all `.json` files to `.ndjson` files, rejecting any file whose
    entry count drops sharply from the last known-good run (see
    MIN_COUNT_RATIO) instead of promoting a truncated/corrupt download.
    This generally takes ~20 seconds to run; memory usage <1MB.
    :return:
    """
    class DecimalEncoder(json.JSONEncoder):
        def default(self, o):
            if isinstance(o, Decimal):
                return float(o)
            return super().default(o)

    counts = _load_counts()

    for data_type in BULK_DATA_TYPES:
        filename = f"{data_type}.json"
        input_path = os.path.join(FOLDER, filename)
        output_path = os.path.join(FOLDER, f"{data_type}.ndjson")
        temp_path = output_path + "_new"

        logger.info(f"Converting {filename} -> {os.path.basename(output_path)}")

        count = 0
        with open(input_path, "rb") as inp, open(temp_path, "w") as out:
            for item in ijson.items(inp, "item"):
                out.write(
                    json.dumps(item, cls=DecimalEncoder) + "\n"
                )
                count += 1

        previous = counts.get(data_type)
        if previous and count < previous * MIN_COUNT_RATIO:
            os.remove(temp_path)
            raise DataSanityError(
                f"{data_type}: new entry count {count} is more than "
                f"{(1 - MIN_COUNT_RATIO):.0%} lower than the last known-good "
                f"count {previous}; refusing to replace {output_path}"
            )

        os.replace(temp_path, output_path)
        logger.info(f"{data_type}: {count} entries (previous: {previous})")
        counts[data_type] = count

    _save_counts(counts)
    return counts

if __name__ == '__main__':
    lock_file = acquire_lock()
    if lock_file is None:
        logger.warning("Another data_updater run is already in progress; exiting.")
        sys.exit(0)

    posthog_enabled = init_posthog()
    sentry_sdk = init_sentry()  # None unless SENTRY_DSN is set and sentry-sdk is installed

    ping_healthcheck("/start")

    # start tracking memory usage
    tracemalloc.start()
    run_start = perf_counter()

    try:
        download_bulk_data()
        counts = convert_json_to_ndjson()
    except Exception as exc:
        logger.exception("data_updater run failed")
        if sentry_sdk is not None:
            sentry_sdk.capture_exception(exc)
        notify_posthog(posthog_enabled, "data_update_failed", {"error": str(exc)})
        ping_healthcheck("/fail")
        sys.exit(1)
    finally:
        current, peak = tracemalloc.get_traced_memory()
        logger.info(f"Current memory usage: {current / 10**6:.2f}MB; Peak memory usage: {peak / 10**6:.2f}MB")
        tracemalloc.stop()

    duration_seconds = perf_counter() - run_start
    notify_posthog(
        posthog_enabled,
        "data_update_succeeded",
        {"counts": counts, "duration_seconds": duration_seconds},
    )
    ping_healthcheck()
