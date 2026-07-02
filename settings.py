"""
Centralized, validated application configuration.

Previously `api.py` and `data_updater.py` each called `os.getenv(...)` directly
and assumed the result was present (e.g. `Path(os.getenv("CARD_JSON_DIR"))`,
`os.getenv("BULK_DATA_TYPES").split(",")`). A missing `.env` entry surfaced as a
cryptic `AttributeError: 'NoneType' object has no attribute 'split'` deep in
unrelated code, and the two modules could silently drift (e.g. different ideas
of where the `cards/` directory is). This module loads `.env` once, validates
everything up front with a clear error message, and is the single source of
truth both modules import from.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"Missing required environment variable: {name!r}")
    return value


def _split_csv(name: str) -> List[str]:
    return [item.strip() for item in _require(name).split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    card_json_dir: Path
    dataset_names: List[str]
    cors_origins: List[str]
    # DESIGN DECISION: when a lookup misses in dataset A, which dataset (if any)
    # should be tried next? Keyed/valued by dataset name, e.g. {"cards": "unique_artwork"}.
    # Configure via DATASET_FALLBACKS="cards:unique_artwork,other:default" in .env.
    dataset_fallbacks: Dict[str, str]

    # --- data_updater.py alerting (all optional; each integration no-ops if
    # its var is unset) ---
    # PostHog: reports data_update_succeeded/data_update_failed events (with
    # per-dataset card counts and run duration) so pipeline runs and card-count
    # growth over time are visible on a dashboard. Wired — set to activate.
    posthog_api_key: Optional[str]
    posthog_host: str
    # healthchecks.io (or any compatible dead-man's-switch): the base ping URL
    # for one check, e.g. https://hc-ping.com/<uuid>. Wired — set to activate.
    # See README for the /start and /fail suffix convention this uses.
    healthcheck_ping_url: Optional[str]
    # Sentry: captures the exception on a failed run. PLUMBED BUT NOT WIRED —
    # sentry-sdk is intentionally not in requirements.txt yet; this is a
    # pending decision, not just a missing key. Add the dependency and set
    # SENTRY_DSN to activate.
    sentry_dsn: Optional[str]

    @classmethod
    def load(cls) -> "Settings":
        card_json_dir = Path(_require("CARD_JSON_DIR"))
        dataset_names = _split_csv("BULK_DATA_TYPES")

        cors_env = os.getenv("CORS_ORIGIN", "").strip()
        cors_origins = [o.strip() for o in cors_env.split(",") if o.strip()] or ["*"]

        dataset_fallbacks: Dict[str, str] = {}
        for pair in os.getenv("DATASET_FALLBACKS", "").split(","):
            pair = pair.strip()
            if not pair:
                continue
            if ":" not in pair:
                raise ConfigError(
                    f"Invalid DATASET_FALLBACKS entry {pair!r}; expected 'from:to'"
                )
            src, dst = (part.strip() for part in pair.split(":", 1))
            dataset_fallbacks[src] = dst

        # Backward-compat: honor the legacy ORACLE_CARDS -> unique_artwork
        # fallback (previously hardcoded in api.py) if DATASET_FALLBACKS wasn't
        # set but ORACLE_CARDS was.
        oracle = os.getenv("ORACLE_CARDS")
        if oracle and oracle not in dataset_fallbacks:
            dataset_fallbacks[oracle] = "unique_artwork"

        return cls(
            card_json_dir=card_json_dir,
            dataset_names=dataset_names,
            cors_origins=cors_origins,
            dataset_fallbacks=dataset_fallbacks,
            posthog_api_key=os.getenv("POSTHOG_API_KEY") or None,
            posthog_host=os.getenv("POSTHOG_HOST", "https://us.i.posthog.com"),
            healthcheck_ping_url=os.getenv("HEALTHCHECK_PING_URL") or None,
            sentry_dsn=os.getenv("SENTRY_DSN") or None,
        )

    def dataset_path(self, name: str) -> Path:
        return self.card_json_dir / f"{name}.ndjson"


settings = Settings.load()
