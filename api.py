import os

import ijson
from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from time import perf_counter
from typing import Dict, List, Optional, Tuple
import json
import logging
import threading
import watchfiles
import asyncio
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CARD_JSON_DIR = Path(os.getenv("CARD_JSON_DIR"))
# Consider making this an env var or CLI arg.

# List of dataset names (no extension). Each must have a matching <name>.ndjson
# in CARD_JSON_DIR. DESIGN DECISION: load this from an env var, config file,
# or CLI argument instead of hardcoding — whatever fits your deployment model.
DATASET_NAMES: List[str] = os.getenv("BULK_DATA_TYPES").split(",")

NDJSON_EXT = ".ndjson"

# Rate limit strings. DESIGN DECISION: tune these per your traffic expectations.
RATE_SINGLE   = "200/second"
RATE_BULK     = "2/second"

# Maximum card IDs accepted in a single bulk request.
BULK_MAX_IDS  = 200

# CORS origins. DESIGN DECISION: pull from env var in production.
CORS_ORIGINS  = ["*"]
# CORS_ORIGINS  = ["http://localhost:5174", "https://aura0.app/", "https://aura-dqp.pages.dev/", "https://y-websocket-test.aura-dqp.pages.dev/"]


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
# Index building (per-dataset)
# ---------------------------------------------------------------------------

@_time_it("Building index")
def build_index(dataset: str) -> None:
    """
    (Re)build the in-memory index for a single dataset file.

    DESIGN DECISION: the keys indexed here are `name` (no spaces) and
    `set+collector_number`. If your other .ndjson files have a different schema
    you will need to either:
      a) define a per-dataset key-extraction function, or
      b) agree on a common schema across all files.
    Add that logic where indicated below.
    """
    path = DATA_FILES[dataset]
    new_index: Dict[str, int] = {}

    logger.info(f"Building index for [{dataset}]")

    with path.open("rb") as f:
        while True:
            offset = f.tell()
            line = f.readline()
            if not line:
                break

            data = json.loads(line)

            # DESIGN DECISION: key extraction per dataset.
            # Option (a) — same schema everywhere:

            # This line skips art cards. We skip art cards because they are not legal
            # e.g. ABLB 31 Mr. Foxglove // Mr. Foxglove
            if data["layout"] == "art_series":
                continue

            name_key = normalize_key(data["name"].split(' // ')[0])
            flavor_name_key = normalize_key(data.get("flavor_name", ''))
            printed_name_key = normalize_key(data.get("printed_name", ''))
            set_key = normalize_key(f'{data["set"]}{data["collector_number"]}')

            # Option (b) — per-dataset key extractor (uncomment and extend):
            # name_key, set_key = _extract_keys(dataset, data)

            if name_key not in new_index:
                new_index[name_key] = offset
            if flavor_name_key != '' and flavor_name_key not in new_index:
                new_index[flavor_name_key] = offset
            if printed_name_key != '' and printed_name_key not in new_index:
                new_index[printed_name_key] = offset
            if set_key not in new_index:
                new_index[set_key] = offset

    indices[dataset].clear()
    indices[dataset].update(new_index)
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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

def scan(card_id):
    with open('cards/unique_artwork.json', 'rb') as f:
        for item in ijson.items(f, "item"):
            if card_id in item['name'] or card_id in item.get('printed_name', []) or card_id in item.get('flavor_name', []):
                return item


def lookup(card_id: str, dataset: Optional[str] = None) -> Optional[dict]:
    ds = _dataset_for_request(dataset)
    try:
        offset = indices[ds].get(normalize_key(card_id))
        if offset is None and ds == os.getenv("ORACLE_CARDS"):
            return lookup(card_id.lower(), 'unique_artwork')
        f = get_handle(ds)
        f.seek(offset)
        return json.loads(f.readline())
    except Exception:
        pass


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

@app.get("/v1/health")
def health():
    if not any(indices.values()):
        raise HTTPException(status_code=503, detail="No indices loaded")
    return {
        "status": "ok",
        "datasets": {name: len(idx) for name, idx in indices.items()},
    }


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