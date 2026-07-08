from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from time import perf_counter
from typing import Callable, Dict, Iterable, List, Optional, Tuple
import json
import logging
import random
import threading
import watchfiles
import asyncio
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi.middleware.cors import CORSMiddleware

from settings import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CARD_JSON_DIR = settings.card_json_dir

# List of dataset names (no extension). Each must have a matching <name>.ndjson
# in CARD_JSON_DIR. Sourced from the BULK_DATA_TYPES env var (see settings.py).
DATASET_NAMES: List[str] = settings.dataset_names

NDJSON_EXT = ".ndjson"

# Rate limit strings. DESIGN DECISION: tune these per your traffic expectations.
RATE_SINGLE   = "200/second"
RATE_BULK     = "2/second"

# Maximum card IDs accepted in a single bulk request.
BULK_MAX_IDS  = 200

# Maximum cards returned by a single /v1/cards/random request.
RANDOM_MAX_N  = 100

# A dataset's underlying .ndjson file is considered stale if it hasn't been
# replaced (by data_updater.py) in longer than this. data_updater.py's own
# docstring targets a 5-7 day refresh cadence, so 10 days gives it a couple
# of missed runs' worth of slack before /v1/health flags it. Reported via
# /v1/health so an external monitor can poll and alert on it.
STALE_AFTER_SECONDS = 10 * 24 * 3600

# CORS origins, sourced from the CORS_ORIGIN env var (see settings.py).
CORS_ORIGINS  = settings.cors_origins


# ---------------------------------------------------------------------------
# Build the file-path registry from DATASET_NAMES
# ---------------------------------------------------------------------------

def _make_data_files() -> Dict[str, Path]:
    """Return {dataset_name: Path} for every name in DATASET_NAMES."""
    return {name: CARD_JSON_DIR / f"{name}{NDJSON_EXT}" for name in DATASET_NAMES}

DATA_FILES: Dict[str, Path] = _make_data_files()


# ---------------------------------------------------------------------------
# Per-dataset indices and thread-local file handles
# ---------------------------------------------------------------------------

# { dataset_name -> { normalized_key -> byte_offset } }
indices: Dict[str, Dict[str, int]] = {name: {} for name in DATASET_NAMES}

# { dataset_name -> [byte_offset, ...] } — one entry per (non-skipped) card,
# built alongside `indices`. Unlike `indices`, which maps several keys (name,
# set+number, ...) to the same card, this holds exactly one offset per card, so
# it can be sampled directly by /v1/cards/random without biasing toward cards
# that happen to have more aliases.
card_offsets: Dict[str, List[int]] = {name: [] for name in DATASET_NAMES}

# Thread-local open file handles: _local.handles = { dataset_name -> file }
_local = threading.local()


def get_handle(dataset: str):
    """Return (and lazily open) a thread-local read handle for *dataset*."""
    if not hasattr(_local, "handles"):
        _local.handles = {}
    handle = _local.handles.get(dataset)
    if handle is None or handle.closed:
        _local.handles[dataset] = DATA_FILES[dataset].open("rb")
    return _local.handles[dataset]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _time_it(title: str):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = perf_counter()
            result = func(*args, **kwargs)
            logger.info(f"{title} took {perf_counter() - start:.6f}s")
            return result
        return wrapper
    return decorator


def normalize_key(raw: str) -> str:
    return raw.strip().lower().replace(" ", "")


# ---------------------------------------------------------------------------
# Per-dataset key extraction (extensibility point)
# ---------------------------------------------------------------------------

def _default_should_skip(data: dict) -> bool:
    # Skip art cards — they are not legal cards.
    # e.g. ABLB 31 Mr. Foxglove // Mr. Foxglove
    # As opposed to combined cards which are named differently.
    # e.g. WOE 7 Cheeky House-Mouse // Squeak By
    # /v1/cards/slD1512 should work though and return the cat/dog sol ring
    return data.get("layout") == "art_series"


def _default_key_extractor(data: dict) -> Iterable[str]:
    """Scryfall-schema key extractor: name, flavor_name, printed_name, set+collector_number."""
    name_key = normalize_key(data["name"].split(' // ')[0])
    flavor_name_key = normalize_key(data.get("flavor_name", ''))
    printed_name_key = normalize_key(data.get("printed_name", ''))
    set_key = normalize_key(f'{data["set"]}{data["collector_number"]}')

    yield name_key
    if flavor_name_key:
        yield flavor_name_key
    if printed_name_key:
        yield printed_name_key
    yield set_key


# DESIGN DECISION: per-dataset extensibility hooks. Every dataset defaults to
# the Scryfall-schema extractor/predicate above. To support a dataset with a
# different schema, override its entry before build_all_indices() runs, e.g.
# KEY_EXTRACTORS["my_dataset"] = my_custom_extractor.
SKIP_PREDICATES: Dict[str, Callable[[dict], bool]] = {
    name: _default_should_skip for name in DATASET_NAMES
}
KEY_EXTRACTORS: Dict[str, Callable[[dict], Iterable[str]]] = {
    name: _default_key_extractor for name in DATASET_NAMES
}


# ---------------------------------------------------------------------------
# Index building (per-dataset)
# ---------------------------------------------------------------------------

@_time_it("Building index")
def build_index(dataset: str) -> None:
    """(Re)build the in-memory index for a single dataset file.

    Keys are produced by SKIP_PREDICATES[dataset] / KEY_EXTRACTORS[dataset] —
    see the registries above for how to support a different schema.
    """
    path = DATA_FILES[dataset]
    new_index: Dict[str, int] = {}
    new_offsets: List[int] = []
    should_skip = SKIP_PREDICATES[dataset]
    extract_keys = KEY_EXTRACTORS[dataset]

    logger.info(f"Building index for [{dataset}]")

    with path.open("rb") as f:
        while True:
            offset = f.tell()
            line = f.readline()
            if not line:
                break

            data = json.loads(line)

            if should_skip(data):
                continue

            new_offsets.append(offset)
            for key in extract_keys(data):
                if key not in new_index:
                    new_index[key] = offset

    # Atomic rebind — readers always look up `indices[dataset]` /
    # `card_offsets[dataset]` fresh, so these swaps never expose a
    # partially-populated structure to a concurrent reader.
    indices[dataset] = new_index
    card_offsets[dataset] = new_offsets
    logger.info(f"[{dataset}] Index built with {len(new_index)} entries.")


def build_all_indices() -> None:
    for name in DATASET_NAMES:
        build_index(name)


# ---------------------------------------------------------------------------
# File watcher — rebuilds only the dataset whose file changed
# ---------------------------------------------------------------------------

async def watch_data_files() -> None:
    paths_to_watch = [str(p) for p in DATA_FILES.values()]
    # Reverse map: absolute path string -> dataset name
    path_to_dataset = {str(p.resolve()): name for name, p in DATA_FILES.items()}

    logger.info(f"Watching: {paths_to_watch}")
    async for changes in watchfiles.awatch(*paths_to_watch):
        for _change_type, changed_path in changes:
            dataset = path_to_dataset.get(str(Path(changed_path).resolve()))
            if dataset:
                logger.info(f"{changed_path} changed — rebuilding [{dataset}]...")
                build_index(dataset)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = [str(p) for p in DATA_FILES.values() if not p.exists()]
    if missing:
        raise RuntimeError(f"Data file(s) not found: {missing}")

    build_all_indices()
    task = asyncio.create_task(watch_data_files())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# App + middleware
# ---------------------------------------------------------------------------

app = FastAPI(title="Card Lookup API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    # Wildcard origins + credentials is rejected by browsers, so only allow
    # credentials when a concrete origin allowlist is configured.
    allow_credentials=CORS_ORIGINS != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Per-IP rate limiting, keyed by request.client.host. Behind a reverse proxy
# this is only the real client IP if uvicorn trusts the proxy's X-Forwarded-For
# (--forwarded-allow-ips); otherwise every request collapses to the proxy IP and
# the limit becomes global. See "Rate limiting behind the proxy" in SETUP.md.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def _dataset_for_request(dataset: Optional[str]) -> str:
    """
    Resolve which dataset to query.

    DESIGN DECISION: when no dataset is specified by the caller, fall back to
    the first entry in DATASET_NAMES. If you want an explicit "default dataset"
    constant, add one. If ambiguity should be an error instead, raise HTTPException here.
    """
    if dataset is not None:
        if dataset not in indices:
            raise HTTPException(status_code=400, detail=f"Unknown dataset: {dataset!r}. "
                                                        f"Valid options: {list(indices)}")
        return dataset
    return DATASET_NAMES[0]  # DESIGN DECISION: default dataset fallback

def lookup(card_id: str, dataset: Optional[str] = None) -> Optional[dict]:
    ds = _dataset_for_request(dataset)
    offset = indices[ds].get(normalize_key(card_id))

    if offset is None:
        # DESIGN DECISION: cross-dataset fallback chain, e.g. an Oracle-only
        # card ID falling back to a unique-artwork dataset. Configure via
        # DATASET_FALLBACKS (see settings.py).
        fallback = settings.dataset_fallbacks.get(ds)
        if fallback and fallback != ds and fallback in indices:
            return lookup(card_id.lower(), fallback)
        return None

    try:
        f = get_handle(ds)
        f.seek(offset)
        return json.loads(f.readline())
    except (OSError, json.JSONDecodeError):
        logger.exception(f"Failed to read card {card_id!r} from dataset [{ds}] at offset {offset}")
        return None


def bulk_lookup(
        card_ids: List[str],
        dataset: Optional[str] = None,
) -> Tuple[List[dict], List[str]]:
    ds = _dataset_for_request(dataset)
    index = indices[ds]
    f = get_handle(ds)
    found, not_found = [], []
    for card_id in card_ids:
        offset = index.get(normalize_key(card_id))
        if offset is None:
            not_found.append(card_id)
        else:
            f.seek(offset)
            found.append(json.loads(f.readline()))
    return found, not_found


def random_cards(n: int, dataset: Optional[str] = None) -> List[dict]:
    """Return *n* distinct random cards from *dataset*.

    Samples without replacement from the dataset's per-card offset list, so the
    result is unbiased (each card equally likely) and free of duplicates. If the
    dataset holds fewer than *n* cards, every card is returned.
    """
    ds = _dataset_for_request(dataset)
    offsets = card_offsets[ds]
    f = get_handle(ds)
    cards = []
    for offset in random.sample(offsets, min(n, len(offsets))):
        f.seek(offset)
        cards.append(json.loads(f.readline()))
    return cards


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class BulkLookupRequest(BaseModel):
    card_ids: List[str] = Field(..., max_length=BULK_MAX_IDS)
    # DESIGN DECISION: expose `dataset` in the bulk request body, or keep it
    # a query param for consistency with the single-card endpoint? Pick one.
    dataset: Optional[str] = Field(
        default=None,
        description="Dataset name to query. Defaults to the primary dataset.",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _dataset_health(name: str, idx: Dict[str, int]) -> dict:
    """Entry count plus freshness, derived from the .ndjson file's mtime so
    it reflects when data_updater.py last replaced it — not when this
    process last rebuilt its in-memory index (which also happens on every
    restart, and would otherwise look falsely fresh after one)."""
    info = {"entries": len(idx), "last_updated": None, "age_seconds": None, "stale": None}
    try:
        mtime = DATA_FILES[name].stat().st_mtime
    except OSError:
        return info
    last_updated = datetime.fromtimestamp(mtime, tz=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - last_updated).total_seconds()
    info.update(
        last_updated=last_updated.isoformat(),
        age_seconds=age_seconds,
        stale=age_seconds > STALE_AFTER_SECONDS,
    )
    return info


@app.get("/v1/health")
def health():
    if not any(indices.values()):
        raise HTTPException(status_code=503, detail="No indices loaded")
    datasets = {name: _dataset_health(name, idx) for name, idx in indices.items()}
    return {
        "status": "degraded" if any(d["stale"] for d in datasets.values()) else "ok",
        "datasets": datasets,
    }


# NOTE: declared before /v1/cards/{card_id} so the literal "random" path isn't
# captured by the {card_id} path parameter.
@app.get("/v1/cards/random")
@limiter.limit(RATE_BULK)
def get_random_cards(
        request: Request,
        n: int = Query(..., ge=1, le=RANDOM_MAX_N,
                       description="Number of random cards to return."),
        dataset: Optional[str] = None,  # e.g. GET /v1/cards/random?n=5&dataset=cards
):
    return {"results": random_cards(n, dataset)}


@app.get("/v1/cards/{card_id}")
@limiter.limit(RATE_SINGLE)
def get_card(
        request: Request,
        card_id: str,
        dataset: Optional[str] = None,  # e.g. GET /cards/lightning-bolt?dataset=cards
):
    result = lookup(card_id, dataset)
    if result is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return result


@app.post("/v1/cards/bulk/lookup")
@limiter.limit(RATE_BULK)
def get_cards_bulk(request: Request, body: BulkLookupRequest):
    found, not_found = bulk_lookup(body.card_ids, body.dataset)
    return {
        "results": found,
        "not_found": not_found,
    }