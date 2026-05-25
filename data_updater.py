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
import json
import os
import tracemalloc
from decimal import Decimal
from functools import wraps
from time import perf_counter

import ijson
import requests

BULK_DATA_TYPES = os.getenv("BULK_DATA_TYPES").split(",")
CHUNK_SIZE = 20 * 1024 * 1024  # 20 MB


def time_it(title):
    def decorator(func):
        @wraps(func)  # Keeps the original function's metadata
        def wrapper(*args, **kwargs):
            start = perf_counter()
            result = func(*args, **kwargs) # Run the actual function
            end = perf_counter()
            print(f"{title} took {end - start:.6f} seconds")
            return result                  # Return the function's result
        return wrapper
    return decorator

@time_it(title="Downloading new cards")
def download_bulk_data():
    """
    Downloads Scryfall bulk cards to `cards.json`.
    This generally takes ~12 seconds to run; memory usage <5MB.
    :return: None
    """
    def get_bulk_data_items() -> dict:
        response = requests.get('https://api.scryfall.com/bulk-data')
        response.raise_for_status()
        return response.json()

    def get_bulk_download_url(bulk_data_type):
        data = get_bulk_data_items()

        for file in data['data']:
            if file['type'] == bulk_data_type:
                return file['download_uri']

        raise ValueError(
            f"No bulk data type '{bulk_data_type}' found. "
            f"Available types: {[f['type'] for f in data['data']]}"
        )

    urls = {data_type: '' for data_type in BULK_DATA_TYPES}
    for bulk_data_type in BULK_DATA_TYPES:
        urls[bulk_data_type] = (get_bulk_download_url(bulk_data_type))

    for data_type, url in urls.items():
        with requests.get(url, stream=True) as response:
            response.raise_for_status()

            with open(f'{data_type}.json', 'wb') as f:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:  # filter out keep-alive chunks
                        f.write(chunk)
                        f.flush()

@time_it(title="Converting JSON to NDJSON")
def convert_json_to_ndjson():
    """
    Converts all `.json` files to `.ndjson` files
    This generally takes ~20 seconds to run; memory usage <1MB.
    :return:
    """
    class DecimalEncoder(json.JSONEncoder):
        def default(self, o):
            if isinstance(o, Decimal):
                return float(o)
            return super().default(o)

    current_dir = os.getcwd()

    for filename in os.listdir(current_dir):
        if filename.endswith(".json"):
            input_path = os.path.join(current_dir, filename)
            output_path = os.path.join(
                current_dir,
                filename[:-5] + ".ndjson"
            )

            print(f"Converting {filename} -> {os.path.basename(output_path)}")

            with open(input_path, "rb") as inp, open(output_path, "w") as out:
                for item in ijson.items(inp, "item"):
                    out.write(
                        json.dumps(item, cls=DecimalEncoder) + "\n"
                    )

if __name__ == '__main__':
    # start tracking memory usage
    tracemalloc.start()

    # do work
    download_bulk_data()
    convert_json_to_ndjson()

    # print memory usage
    current, peak = tracemalloc.get_traced_memory()
    print(f"Current memory usage: {current / 10**6:.2f}MB; Peak memory usage: {peak / 10**6:.2f}MB")
    tracemalloc.stop()
