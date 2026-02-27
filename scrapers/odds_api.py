"""Fetch betting odds from the-odds-api.com."""

import time
import logging
from datetime import datetime

import requests

import config
from scrapers.cache import get_cached, set_cached

logger = logging.getLogger(__name__)


def _normalize_name(name):
    """Normalize fighter name for matching."""
    return name.strip().lower().replace(".", "").replace("-", " ")


def fetch_odds():
    """Fetch current MMA odds from the-odds-api.com.

    Returns list of dicts:
    [
        {
            "fighter1": "...", "fighter2": "...",
            "bookmakers": [
                {"key": "draftkings", "fighter1_ml": -150, "fighter2_ml": 130, ...}
            ]
        },
        ...
    ]
    """
    if not config.ODDS_API_KEY:
        logger.warning(
            "No ODDS_API_KEY set. Set the ODDS_API_KEY environment variable "
            "with your free key from https://the-odds-api.com/"
        )
        return []

    url = (
        f"{config.ODDS_API_BASE}/sports/{config.ODDS_SPORT}/odds/"
        f"?apiKey={config.ODDS_API_KEY}"
        f"&regions={config.ODDS_REGIONS}"
        f"&markets={config.ODDS_MARKETS}"
        f"&oddsFormat=american"
    )

    # Use a separate cache key without the API key
    cache_key = f"odds_api_{config.ODDS_SPORT}_{config.ODDS_REGIONS}"
    cached = get_cached(cache_key)
    if cached:
        import json
        try:
            return json.loads(cached)
        except Exception:
            pass

    logger.info("Fetching odds from the-odds-api.com")
    try:
        resp = requests.get(url, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Odds API request failed: %s", e)
        return []

    data = resp.json()
    remaining = resp.headers.get("x-requests-remaining", "?")
    logger.info("Odds API requests remaining: %s", remaining)

    results = []
    for event in data:
        fight_info = {
            "fighter1": event.get("home_team", ""),
            "fighter2": event.get("away_team", ""),
            "commence_time": event.get("commence_time", ""),
            "bookmakers": [],
        }

        for bm in event.get("bookmakers", []):
            book = {
                "key": bm.get("key", ""),
                "title": bm.get("title", ""),
                "last_update": bm.get("last_update", ""),
            }

            for market in bm.get("markets", []):
                if market["key"] == "h2h":
                    for outcome in market.get("outcomes", []):
                        name = outcome["name"]
                        price = outcome["price"]
                        if _normalize_name(name) == _normalize_name(fight_info["fighter1"]):
                            book["fighter1_ml"] = price
                        elif _normalize_name(name) == _normalize_name(fight_info["fighter2"]):
                            book["fighter2_ml"] = price

                elif market["key"] == "totals":
                    for outcome in market.get("outcomes", []):
                        point = outcome.get("point")
                        price = outcome["price"]
                        if outcome["name"] == "Over":
                            book["over_rounds"] = point
                            book["over_price"] = price
                        elif outcome["name"] == "Under":
                            book["under_rounds"] = point
                            book["under_price"] = price

            fight_info["bookmakers"].append(book)

        results.append(fight_info)

    # Cache the processed results
    import json
    set_cached(cache_key, json.dumps(results))

    return results


def american_to_implied(odds):
    """Convert American odds to implied probability."""
    if odds is None:
        return None
    if odds > 0:
        return 100.0 / (odds + 100)
    else:
        return abs(odds) / (abs(odds) + 100)


def implied_to_american(prob):
    """Convert implied probability to American odds."""
    if prob is None or prob <= 0 or prob >= 1:
        return None
    if prob >= 0.5:
        return int(-prob / (1 - prob) * 100)
    else:
        return int((1 - prob) / prob * 100)


def get_best_odds(fight_odds):
    """Given a fight's odds data, find best available line for each fighter.

    Returns dict with best fighter1/fighter2 moneylines and implied probs.
    """
    best_f1_ml = None
    best_f2_ml = None
    best_f1_book = None
    best_f2_book = None

    for bm in fight_odds.get("bookmakers", []):
        f1_ml = bm.get("fighter1_ml")
        f2_ml = bm.get("fighter2_ml")

        if f1_ml is not None:
            if best_f1_ml is None or f1_ml > best_f1_ml:
                best_f1_ml = f1_ml
                best_f1_book = bm.get("title", bm.get("key", ""))

        if f2_ml is not None:
            if best_f2_ml is None or f2_ml > best_f2_ml:
                best_f2_ml = f2_ml
                best_f2_book = bm.get("title", bm.get("key", ""))

    consensus_f1_mls = [
        bm["fighter1_ml"] for bm in fight_odds.get("bookmakers", [])
        if bm.get("fighter1_ml") is not None
    ]
    consensus_f2_mls = [
        bm["fighter2_ml"] for bm in fight_odds.get("bookmakers", [])
        if bm.get("fighter2_ml") is not None
    ]

    avg_f1_ml = sum(consensus_f1_mls) / len(consensus_f1_mls) if consensus_f1_mls else None
    avg_f2_ml = sum(consensus_f2_mls) / len(consensus_f2_mls) if consensus_f2_mls else None

    return {
        "best_f1_ml": best_f1_ml,
        "best_f2_ml": best_f2_ml,
        "best_f1_book": best_f1_book,
        "best_f2_book": best_f2_book,
        "avg_f1_ml": avg_f1_ml,
        "avg_f2_ml": avg_f2_ml,
        "f1_implied": american_to_implied(avg_f1_ml),
        "f2_implied": american_to_implied(avg_f2_ml),
        "num_books": len(fight_odds.get("bookmakers", [])),
    }


def store_odds_snapshot(conn, fight_odds_list):
    """Store current odds snapshot and append to history."""
    now = datetime.utcnow().isoformat()
    for fight in fight_odds_list:
        f1 = fight.get("fighter1", "")
        f2 = fight.get("fighter2", "")
        for bm in fight.get("bookmakers", []):
            # Current odds
            conn.execute(
                """INSERT OR REPLACE INTO odds
                   (fighter1_name, fighter2_name, sportsbook,
                    fighter1_ml, fighter2_ml,
                    over_under_rounds, over_price, under_price, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f1, f2, bm.get("key", ""),
                    bm.get("fighter1_ml"), bm.get("fighter2_ml"),
                    bm.get("over_rounds"), bm.get("over_price"),
                    bm.get("under_price"), now,
                )
            )
            # History
            conn.execute(
                """INSERT INTO odds_history
                   (fighter1_name, fighter2_name, sportsbook,
                    fighter1_ml, fighter2_ml, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    f1, f2, bm.get("key", ""),
                    bm.get("fighter1_ml"), bm.get("fighter2_ml"), now,
                )
            )
