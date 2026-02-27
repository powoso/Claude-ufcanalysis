"""ELO rating system adapted for MMA — separate striking, grappling, cardio."""

import math
import logging
from datetime import datetime

import config

logger = logging.getLogger(__name__)


def expected_score(rating_a, rating_b):
    """Standard ELO expected score for player A."""
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400))


def update_rating(rating, expected, actual, k=None):
    """Update a single ELO rating."""
    if k is None:
        k = config.ELO_K_FACTOR
    return rating + k * (actual - expected)


def _method_bonuses(method):
    """Return per-domain bonus multipliers for the win method."""
    method = (method or "").lower()
    if "ko" in method or "tko" in method:
        return {"striking": 1.5, "grappling": 0.3, "cardio": 0.8}
    elif "sub" in method:
        return {"striking": 0.3, "grappling": 1.5, "cardio": 0.8}
    elif "dec" in method:
        # decisions reward cardio; moderate on skills
        return {"striking": 0.8, "grappling": 0.8, "cardio": 1.4}
    return {"striking": 1.0, "grappling": 1.0, "cardio": 1.0}


def _round_bonus(rnd, method):
    """Bonus K factor for early finishes."""
    method = (method or "").lower()
    if "dec" in method:
        return 1.0
    if rnd is None:
        return 1.0
    if rnd == 1:
        return 1.3
    elif rnd == 2:
        return 1.15
    return 1.0


def compute_all_elo(conn):
    """Recompute all ELO ratings from scratch using fight history.

    Processes fights in chronological order. Each fighter gets:
    - overall: aggregate rating
    - striking: adjusted for KO/TKO wins and losses
    - grappling: adjusted for submission wins
    - cardio: adjusted for decision outcomes (going the distance)
    """
    logger.info("Computing ELO ratings from scratch...")

    # Clear existing ratings
    conn.execute("DELETE FROM elo_ratings")

    # Get all completed fights in chronological order
    fights = conn.execute(
        """SELECT f.*, e.date as event_date
           FROM fights f
           JOIN events e ON f.event_id = e.id
           WHERE f.winner_id IS NOT NULL AND e.date IS NOT NULL
           ORDER BY e.date ASC, f.id ASC"""
    ).fetchall()

    # In-memory rating store
    ratings = {}  # fighter_id -> {overall, striking, grappling, cardio}

    def get_rating(fid):
        if fid not in ratings:
            ratings[fid] = {
                "overall": config.ELO_INITIAL,
                "striking": config.ELO_INITIAL,
                "grappling": config.ELO_INITIAL,
                "cardio": config.ELO_INITIAL,
            }
        return ratings[fid]

    for fight in fights:
        f1_id = fight["fighter1_id"]
        f2_id = fight["fighter2_id"]
        winner_id = fight["winner_id"]
        method = fight["method"]
        rnd = fight["round"]

        r1 = get_rating(f1_id)
        r2 = get_rating(f2_id)

        # Determine actual scores
        if winner_id == f1_id:
            actual_1, actual_2 = 1.0, 0.0
        elif winner_id == f2_id:
            actual_1, actual_2 = 0.0, 1.0
        else:
            actual_1, actual_2 = 0.5, 0.5  # draw

        bonuses = _method_bonuses(method)
        rd_bonus = _round_bonus(rnd, method)

        for domain in ["striking", "grappling", "cardio"]:
            exp1 = expected_score(r1[domain], r2[domain])
            exp2 = expected_score(r2[domain], r1[domain])
            k = config.ELO_K_FACTOR * bonuses[domain] * rd_bonus
            r1[domain] = update_rating(r1[domain], exp1, actual_1, k)
            r2[domain] = update_rating(r2[domain], exp2, actual_2, k)

        # Overall: weighted combination
        exp1_o = expected_score(r1["overall"], r2["overall"])
        exp2_o = expected_score(r2["overall"], r1["overall"])
        k_o = config.ELO_K_FACTOR * rd_bonus
        r1["overall"] = update_rating(r1["overall"], exp1_o, actual_1, k_o)
        r2["overall"] = update_rating(r2["overall"], exp2_o, actual_2, k_o)

    # Store final ratings
    now = datetime.utcnow().isoformat()
    for fid, r in ratings.items():
        conn.execute(
            """INSERT INTO elo_ratings
               (fighter_id, overall, striking, grappling, cardio, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (fid, r["overall"], r["striking"], r["grappling"], r["cardio"], now)
        )

    logger.info("ELO ratings computed for %d fighters", len(ratings))
    return ratings


def get_fighter_elo(conn, fighter_id):
    """Get the latest ELO rating for a fighter."""
    row = conn.execute(
        """SELECT * FROM elo_ratings WHERE fighter_id = ?
           ORDER BY updated_at DESC LIMIT 1""",
        (fighter_id,)
    ).fetchone()
    if row:
        return dict(row)
    return {
        "overall": config.ELO_INITIAL,
        "striking": config.ELO_INITIAL,
        "grappling": config.ELO_INITIAL,
        "cardio": config.ELO_INITIAL,
    }


def elo_win_probability(rating_a, rating_b):
    """ELO-based win probability for fighter A."""
    return expected_score(rating_a, rating_b)
