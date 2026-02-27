"""Simple file-based cache for HTTP responses."""

import os
import hashlib
import json
import time

import config


def _cache_path(url):
    h = hashlib.sha256(url.encode()).hexdigest()[:16]
    safe = "".join(c if c.isalnum() else "_" for c in url[-60:])
    return os.path.join(config.CACHE_DIR, f"{safe}_{h}.json")


def get_cached(url):
    """Return cached response text if fresh, else None."""
    path = _cache_path(url)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
        age_hours = (time.time() - data["ts"]) / 3600
        if age_hours < config.CACHE_TTL_HOURS:
            return data["body"]
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def set_cached(url, body):
    """Save response text to cache."""
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    path = _cache_path(url)
    with open(path, "w") as f:
        json.dump({"url": url, "ts": time.time(), "body": body}, f)
