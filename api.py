from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from time import perf_counter
from typing import Dict, List, Optional
import json
import logging
import threading
import watchfiles
import asyncio
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not CARDS_FILE.exists():
        raise RuntimeError(f"Data file not found: {CARDS_FILE}")
    build_indices()
    task = asyncio.create_task(watch_cards_file())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Card Lookup API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CARDS_FILE = Path("cards.ndjson")
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Maps normalized lookup keys -> byte offset in file
card_index: Dict[str, int] = {}

# Thread-local file handles
_local = threading.local()

def get_file():
    if not hasattr(_local, "f") or _local.f.closed:
        _local.f = CARDS_FILE.open("rb")
    return _local.f

def _time_it(title):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = perf_counter()
            result = func(*args, **kwargs)
            end = perf_counter()
            logger.info(f"{title} took {end - start:.6f} seconds")
            return result
        return wrapper
    return decorator

def normalize_card_id(card_id: str) -> str:
    return card_id.strip().lower()

@_time_it(title="Building indices")
def build_indices():
    new_index = {}
    with CARDS_FILE.open("rb") as f:
        while True:
            offset = f.tell()
            line = f.readline()
            if not line:
                break
            try:
                data = json.loads(line)
                name_key = normalize_card_id(data["name"]).replace(" ", "")
                set_key = normalize_card_id(
                    f'{data["set"]}{data["collector_number"]}'
                ).replace(" ", "")
                if name_key not in new_index:
                    new_index[name_key] = offset
                if set_key not in new_index:
                    new_index[set_key] = offset
            except (KeyError, json.JSONDecodeError) as e:
                logger.warning(f"Skipping invalid line at offset {offset}: {e}")
    card_index.clear()
    card_index.update(new_index)
    logger.info(f"Index built with {len(card_index)} entries.")

async def watch_cards_file():
    logger.info(f"Watching {CARDS_FILE} for changes...")
    async for _ in watchfiles.awatch(CARDS_FILE):
        logger.info(f"{CARDS_FILE} changed, rebuilding index...")
        build_indices()

def lookup(card_id: str) -> Optional[dict]:
    normalized = normalize_card_id(card_id).replace(" ", "")
    offset = card_index.get(normalized.lower())
    if offset is None:
        return None
    f = get_file()
    f.seek(offset)
    return json.loads(f.readline())

def bulk_lookup(card_ids: List[str]) -> tuple[List[dict], List[str]]:
    found = []
    not_found = []
    f = get_file()
    for card_id in card_ids:
        normalized = normalize_card_id(card_id).replace(" ", "")
        offset = card_index.get(normalized)
        if offset is None:
            not_found.append(card_id)
            continue
        f.seek(offset)
        found.append(json.loads(f.readline()))
    return found, not_found

class BulkLookupRequest(BaseModel):
    card_ids: List[str] = Field(..., max_length=100)

@app.get("/health")
def health():
    if len(card_index) == 0:
        raise HTTPException(status_code=503, detail="Index not loaded")
    return {
        "status": "ok",
        "indexed_cards": len(card_index),
    }

@app.get("/cards/{card_id}")
@limiter.limit("200/second")
def get_card(request: Request, card_id: str):
    result = lookup(card_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return result

@app.post("/cards/bulk/lookup")
@limiter.limit("2/second")
def get_cards_bulk(request: Request, body: BulkLookupRequest):
    found, not_found = bulk_lookup(body.card_ids)
    return {
        "results": found,
        "not_found": not_found,
    }