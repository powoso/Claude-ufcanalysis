"""Prop bet analysis — round totals and method of victory."""

import logging

import config
from analysis.fighter_profile import build_fighter_profile, compute_weighted_stats
from scrapers.odds_api import american_to_implied

logger = logging.getLogger(__name__)


def method_of_victory_probs(conn, fighter1_id, fighter2_id):
    """Estimate method of victory probabilities for a fight.

    Returns:
        {
            "fighter1_ko_prob": ...,
            "fighter1_sub_prob": ...,
            "fighter1_dec_prob": ...,
            "fighter2_ko_prob": ...,
            "fighter2_sub_prob": ...,
            "fighter2_dec_prob": ...,
            "ko_prob": ...,   # overall KO
            "sub_prob": ...,  # overall Sub
            "dec_prob": ...,  # overall Decision
        }
    """
    s1 = compute_weighted_stats(conn, fighter1_id)
    s2 = compute_weighted_stats(conn, fighter2_id)

    if not s1 or not s2:
        return None

    # Base method rates per fighter, averaged
    f1_ko = s1.get("ko_rate", 0.33)
    f1_sub = s1.get("sub_rate", 0.15)
    f1_dec = s1.get("dec_rate", 0.52)

    f2_ko = s2.get("ko_rate", 0.33)
    f2_sub = s2.get("sub_rate", 0.15)
    f2_dec = s2.get("dec_rate", 0.52)

    # Overall fight method probability (averaged between both fighters)
    # This is a simplification — assumes both fighters' tendencies combine
    overall_ko = (f1_ko + f2_ko) / 2.0
    overall_sub = (f1_sub + f2_sub) / 2.0
    overall_dec = (f1_dec + f2_dec) / 2.0

    # Normalize
    total = overall_ko + overall_sub + overall_dec
    if total > 0:
        overall_ko /= total
        overall_sub /= total
        overall_dec /= total

    # Adjust for specific matchup dynamics
    # High striking differential → more likely KO
    str_diff = abs(s1.get("sig_str_landed_avg", 0) - s2.get("sig_str_landed_avg", 0))
    if str_diff > 2.0:
        ko_boost = min(0.1, str_diff * 0.02)
        overall_ko += ko_boost
        overall_dec -= ko_boost

    # High grappling differential → more likely sub
    grap_diff = abs(s1.get("sub_att_avg", 0) - s2.get("sub_att_avg", 0))
    if grap_diff > 0.5:
        sub_boost = min(0.08, grap_diff * 0.04)
        overall_sub += sub_boost
        overall_dec -= sub_boost

    # Re-normalize
    total = overall_ko + overall_sub + overall_dec
    if total > 0:
        overall_ko /= total
        overall_sub /= total
        overall_dec /= total

    return {
        "fighter1_ko_rate": f1_ko,
        "fighter1_sub_rate": f1_sub,
        "fighter1_dec_rate": f1_dec,
        "fighter2_ko_rate": f2_ko,
        "fighter2_sub_rate": f2_sub,
        "fighter2_dec_rate": f2_dec,
        "ko_prob": overall_ko,
        "sub_prob": overall_sub,
        "dec_prob": overall_dec,
    }


def over_under_analysis(conn, fighter1_id, fighter2_id, posted_total=None):
    """Analyze over/under round totals.

    Uses fighter finish rates, average fight length, and pace to estimate
    probability of fight going over or under a given round total.

    Returns dict with probabilities and any identified edge.
    """
    s1 = compute_weighted_stats(conn, fighter1_id)
    s2 = compute_weighted_stats(conn, fighter2_id)

    if not s1 or not s2:
        return None

    # Average rounds fought by each fighter
    avg_rounds_1 = s1.get("avg_rounds", 2.5)
    avg_rounds_2 = s2.get("avg_rounds", 2.5)

    # Finish rates
    finish_1 = s1.get("finish_rate", 0.5)
    finish_2 = s2.get("finish_rate", 0.5)

    # Combined expected fight length (simple average)
    expected_rounds = (avg_rounds_1 + avg_rounds_2) / 2.0

    # Probability of finish (either fighter) — don't double-count
    # P(at least one finishes) = 1 - P(neither finishes)
    combined_finish = 1.0 - (1.0 - finish_1) * (1.0 - finish_2)

    # Default total line is usually 2.5 rounds
    if posted_total is None:
        posted_total = 2.5

    # Estimate P(over) using a simple model:
    # If expected rounds > total → lean over
    # Higher finish rate → lean under
    # This is a simplified approach; a more sophisticated model would use
    # per-round finish probabilities

    # Base probability from expected rounds
    rounds_diff = expected_rounds - posted_total
    base_over = 0.5 + rounds_diff * 0.15  # sensitivity parameter

    # Adjust for finish rate
    finish_adjustment = (combined_finish - 0.5) * -0.2
    over_prob = max(0.05, min(0.95, base_over + finish_adjustment))
    under_prob = 1.0 - over_prob

    return {
        "expected_rounds": expected_rounds,
        "combined_finish_rate": combined_finish,
        "posted_total": posted_total,
        "over_prob": over_prob,
        "under_prob": under_prob,
        "fighter1_avg_rounds": avg_rounds_1,
        "fighter2_avg_rounds": avg_rounds_2,
        "fighter1_finish_rate": finish_1,
        "fighter2_finish_rate": finish_2,
    }


def analyze_props(conn, fighter1_id, fighter2_id, fight_odds=None):
    """Full prop bet analysis for a fight.

    Returns dict with method of victory and over/under analysis,
    plus any identified edges vs posted lines.
    """
    mov = method_of_victory_probs(conn, fighter1_id, fighter2_id)

    # Get posted over/under if available
    posted_total = None
    posted_over_price = None
    posted_under_price = None
    if fight_odds:
        for bm in fight_odds.get("bookmakers", []):
            if bm.get("over_rounds"):
                posted_total = bm["over_rounds"]
                posted_over_price = bm.get("over_price")
                posted_under_price = bm.get("under_price")
                break

    ou = over_under_analysis(conn, fighter1_id, fighter2_id, posted_total)

    prop_edges = []

    # Check over/under edges
    if ou and posted_over_price is not None:
        over_implied = american_to_implied(posted_over_price)
        under_implied = american_to_implied(posted_under_price)

        if over_implied and ou["over_prob"] - over_implied >= config.EDGE_THRESHOLD:
            prop_edges.append({
                "type": "Over",
                "value": f"Over {posted_total} rounds",
                "model_prob": ou["over_prob"],
                "market_implied": over_implied,
                "edge": ou["over_prob"] - over_implied,
                "odds": posted_over_price,
            })

        if under_implied and ou["under_prob"] - under_implied >= config.EDGE_THRESHOLD:
            prop_edges.append({
                "type": "Under",
                "value": f"Under {posted_total} rounds",
                "model_prob": ou["under_prob"],
                "market_implied": under_implied,
                "edge": ou["under_prob"] - under_implied,
                "odds": posted_under_price,
            })

    return {
        "method_of_victory": mov,
        "over_under": ou,
        "prop_edges": prop_edges,
    }
