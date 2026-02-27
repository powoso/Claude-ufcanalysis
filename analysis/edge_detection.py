"""Edge detection and Kelly Criterion position sizing."""

import math
import logging
from datetime import datetime

import config
from scrapers.odds_api import american_to_implied, implied_to_american, get_best_odds
from models.predictor import predict_fight

logger = logging.getLogger(__name__)


def kelly_criterion(win_prob, odds_american):
    """Calculate Kelly Criterion bet size.

    Returns fraction of bankroll to bet (0 if negative edge).
    Uses fractional Kelly per config.KELLY_FRACTION.
    """
    if odds_american is None or win_prob is None:
        return 0.0
    if win_prob <= 0 or win_prob >= 1:
        return 0.0

    # Convert to decimal odds
    if odds_american > 0:
        decimal_odds = odds_american / 100.0 + 1.0
    else:
        decimal_odds = 100.0 / abs(odds_american) + 1.0

    b = decimal_odds - 1.0  # net payout per $1
    p = win_prob
    q = 1.0 - p

    # Kelly formula: f* = (bp - q) / b
    if b <= 0:
        return 0.0
    full_kelly = (b * p - q) / b

    if full_kelly <= 0:
        return 0.0

    return full_kelly * config.KELLY_FRACTION


def calculate_edge(model_prob, market_implied_prob):
    """Calculate the edge: model probability minus market implied probability."""
    if model_prob is None or market_implied_prob is None:
        return 0.0
    return model_prob - market_implied_prob


def classify_confidence(edge, kelly_size):
    """Classify a bet's confidence level."""
    if abs(edge) >= 0.15 and kelly_size >= 0.04:
        return "high"
    elif abs(edge) >= 0.08 and kelly_size >= 0.02:
        return "medium"
    elif abs(edge) >= config.EDGE_THRESHOLD:
        return "low"
    return "none"


def find_edges(conn, fight_odds_list, pipeline=None):
    """Find +EV betting edges for all upcoming fights.

    Compares model-implied odds to market odds and identifies edges
    where the model disagrees by more than EDGE_THRESHOLD.

    Returns list of edge dicts sorted by expected value.
    """
    edges = []

    for fight_odds in fight_odds_list:
        f1_name = fight_odds.get("fighter1", "")
        f2_name = fight_odds.get("fighter2", "")

        if not f1_name or not f2_name:
            continue

        # Try to match fighters in the database
        from database import get_fighter_by_name
        f1 = get_fighter_by_name(conn, f1_name)
        f2 = get_fighter_by_name(conn, f2_name)

        if not f1 or not f2:
            logger.debug("Could not match fighters: %s vs %s", f1_name, f2_name)
            continue

        # Get model prediction
        pred = predict_fight(conn, f1["id"], f2["id"], pipeline)
        if not pred:
            continue

        # Get best available odds
        best = get_best_odds(fight_odds)
        if not best.get("f1_implied") or not best.get("f2_implied"):
            continue

        # Check fighter 1 edge
        edge_f1 = calculate_edge(pred["fighter1_win_prob"], best["f1_implied"])
        kelly_f1 = kelly_criterion(pred["fighter1_win_prob"], best.get("best_f1_ml"))
        conf_f1 = classify_confidence(edge_f1, kelly_f1)

        if edge_f1 >= config.EDGE_THRESHOLD:
            ev_f1 = _expected_value(pred["fighter1_win_prob"], best.get("best_f1_ml"))
            edges.append({
                "fighter_name": f1_name,
                "opponent_name": f2_name,
                "fighter_id": f1["id"],
                "opponent_id": f2["id"],
                "model_prob": pred["fighter1_win_prob"],
                "market_implied": best["f1_implied"],
                "edge": edge_f1,
                "best_odds": best.get("best_f1_ml"),
                "best_book": best.get("best_f1_book"),
                "avg_odds": best.get("avg_f1_ml"),
                "kelly_fraction": kelly_f1,
                "kelly_units": kelly_f1 * config.BANKROLL,
                "confidence": conf_f1,
                "expected_value": ev_f1,
                "elo_prob": pred.get("elo_prob"),
                "model_only_prob": pred.get("model_prob"),
                "num_books": best.get("num_books", 0),
            })

        # Check fighter 2 edge
        edge_f2 = calculate_edge(pred["fighter2_win_prob"], best["f2_implied"])
        kelly_f2 = kelly_criterion(pred["fighter2_win_prob"], best.get("best_f2_ml"))
        conf_f2 = classify_confidence(edge_f2, kelly_f2)

        if edge_f2 >= config.EDGE_THRESHOLD:
            ev_f2 = _expected_value(pred["fighter2_win_prob"], best.get("best_f2_ml"))
            edges.append({
                "fighter_name": f2_name,
                "opponent_name": f1_name,
                "fighter_id": f2["id"],
                "opponent_id": f1["id"],
                "model_prob": pred["fighter2_win_prob"],
                "market_implied": best["f2_implied"],
                "edge": edge_f2,
                "best_odds": best.get("best_f2_ml"),
                "best_book": best.get("best_f2_book"),
                "avg_odds": best.get("avg_f2_ml"),
                "kelly_fraction": kelly_f2,
                "kelly_units": kelly_f2 * config.BANKROLL,
                "confidence": conf_f2,
                "expected_value": ev_f2,
                "elo_prob": 1.0 - pred.get("elo_prob", 0.5),
                "model_only_prob": 1.0 - pred.get("model_prob", 0.5),
                "num_books": best.get("num_books", 0),
            })

    # Sort by expected value descending
    edges.sort(key=lambda x: x["expected_value"], reverse=True)
    return edges


def _expected_value(win_prob, odds_american):
    """Calculate expected value per unit bet."""
    if odds_american is None or win_prob is None:
        return 0.0
    if odds_american > 0:
        payout = odds_american / 100.0
    else:
        payout = 100.0 / abs(odds_american)
    return win_prob * payout - (1.0 - win_prob)


def store_predictions(conn, edges, event_name=""):
    """Store prediction records in the database."""
    now = datetime.utcnow().isoformat()
    for edge in edges:
        # Find matching fight
        fight = conn.execute(
            """SELECT f.id FROM fights f
               WHERE (f.fighter1_id = ? AND f.fighter2_id = ?)
                  OR (f.fighter1_id = ? AND f.fighter2_id = ?)
               ORDER BY f.id DESC LIMIT 1""",
            (edge["fighter_id"], edge["opponent_id"],
             edge["opponent_id"], edge["fighter_id"])
        ).fetchone()

        fight_id = fight["id"] if fight else None
        if not fight_id:
            continue

        conn.execute(
            """INSERT INTO predictions
               (fight_id, fighter1_win_prob, fighter2_win_prob,
                predicted_winner_id, edge_fighter1, kelly_fighter1,
                confidence, model_version, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fight_id,
                edge["model_prob"],
                1.0 - edge["model_prob"],
                edge["fighter_id"],
                edge["edge"],
                edge["kelly_fraction"],
                edge["confidence"],
                "v1.0",
                now,
            )
        )
