"""Fighter analysis engine — weighted stats, style matchups, and profiles."""

import math
import logging
from datetime import datetime, date

import config

logger = logging.getLogger(__name__)


def calculate_age(dob_str):
    """Calculate age from DOB string (YYYY-MM-DD)."""
    if not dob_str:
        return None
    try:
        dob = datetime.strptime(dob_str, "%Y-%m-%d").date()
        today = date.today()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except ValueError:
        return None


def days_since_last_fight(conn, fighter_id):
    """Calculate days since fighter's last bout."""
    row = conn.execute(
        """SELECT e.date FROM fights f
           JOIN events e ON f.event_id = e.id
           WHERE (f.fighter1_id = ? OR f.fighter2_id = ?) AND e.date IS NOT NULL
           ORDER BY e.date DESC LIMIT 1""",
        (fighter_id, fighter_id)
    ).fetchone()
    if not row or not row["date"]:
        return None
    try:
        last = datetime.strptime(row["date"], "%Y-%m-%d").date()
        return (date.today() - last).days
    except ValueError:
        return None


def _career_stats_fallback(conn, fighter_id):
    """Build a stats dict from the fighter_stats career averages table.

    Used when no individual fight records with results are available,
    so the model can still make predictions using career averages
    scraped from the fighter's UFCStats profile page.
    """
    career = conn.execute(
        "SELECT * FROM fighter_stats WHERE fighter_id = ?", (fighter_id,)
    ).fetchone()
    fighter = conn.execute(
        "SELECT record_wins, record_losses, record_draws FROM fighters WHERE id = ?",
        (fighter_id,)
    ).fetchone()

    if not career and not fighter:
        return None

    wins = (fighter["record_wins"] or 0) if fighter else 0
    losses = (fighter["record_losses"] or 0) if fighter else 0
    total = wins + losses
    win_rate = wins / total if total > 0 else 0.5

    result = {
        "num_fights": total,
        "weighted_win_rate": win_rate,
        "sig_str_landed_avg": 0,
        "sig_str_accuracy": 0,
        "td_landed_avg": 0,
        "td_accuracy": 0,
        "sub_att_avg": 0,
        "ctrl_time_avg": 0,
        "knockdowns_avg": 0,
        "ko_rate": 0,
        "sub_rate": 0,
        "dec_rate": 0,
        "finish_rate": 0,
        "avg_rounds": 0,
    }

    if career:
        # Map career averages (per-minute) to approximate per-fight values
        # Assume ~15 min avg fight (3 rounds)
        avg_minutes = 15
        result["sig_str_landed_avg"] = (career.get("slpm") or 0) * avg_minutes
        result["sig_str_accuracy"] = career.get("str_acc") or 0
        result["td_landed_avg"] = (career.get("td_avg") or 0)
        result["td_accuracy"] = career.get("td_acc") or 0
        result["sub_att_avg"] = career.get("sub_avg") or 0
        result["ko_rate"] = career.get("ko_rate") or 0
        result["sub_rate"] = career.get("sub_rate") or 0
        result["dec_rate"] = career.get("dec_rate") or 0
        result["finish_rate"] = career.get("finish_rate") or 0

    return result


def compute_weighted_stats(conn, fighter_id, max_fights=10):
    """Compute recency-weighted stats from individual fight records.

    The last N fights are weighted using exponential decay, with the most
    recent fight getting weight 1.0, second most recent 0.85, etc.

    Falls back to career stats from fighter_stats table if no completed
    fights with results are found.
    """
    fights = conn.execute(
        """SELECT f.id, f.fighter1_id, f.fighter2_id, f.winner_id,
                  f.method, f.round, f.time, e.date as event_date
           FROM fights f
           JOIN events e ON f.event_id = e.id
           WHERE (f.fighter1_id = ? OR f.fighter2_id = ?)
                 AND f.winner_id IS NOT NULL
                 AND e.date IS NOT NULL
           ORDER BY e.date DESC
           LIMIT ?""",
        (fighter_id, fighter_id, max_fights)
    ).fetchall()

    if not fights:
        # Fall back to career stats from fighter_stats table
        return _career_stats_fallback(conn, fighter_id)

    total_weight = 0
    weighted = {
        "sig_str_landed": 0, "sig_str_attempted": 0,
        "td_landed": 0, "td_attempted": 0,
        "sub_att": 0, "ctrl_time": 0,
        "knockdowns": 0, "total_str_landed": 0,
        "wins": 0, "losses": 0,
        "ko_wins": 0, "sub_wins": 0, "dec_wins": 0,
        "rounds_fought": 0,
    }

    for i, fight in enumerate(fights):
        weight = config.RECENCY_DECAY ** i
        total_weight += weight

        # Get per-fight stats
        fs = conn.execute(
            "SELECT * FROM fight_stats WHERE fight_id = ? AND fighter_id = ?",
            (fight["id"], fighter_id)
        ).fetchone()

        won = fight["winner_id"] == fighter_id
        method = (fight["method"] or "").lower()

        if won:
            weighted["wins"] += weight
            if "ko" in method or "tko" in method:
                weighted["ko_wins"] += weight
            elif "sub" in method:
                weighted["sub_wins"] += weight
            else:
                weighted["dec_wins"] += weight
        else:
            weighted["losses"] += weight

        rnd = fight["round"] or 3
        weighted["rounds_fought"] += rnd * weight

        if fs:
            weighted["sig_str_landed"] += (fs["sig_str_landed"] or 0) * weight
            weighted["sig_str_attempted"] += (fs["sig_str_attempted"] or 0) * weight
            weighted["td_landed"] += (fs["td_landed"] or 0) * weight
            weighted["td_attempted"] += (fs["td_attempted"] or 0) * weight
            weighted["sub_att"] += (fs["sub_att"] or 0) * weight
            weighted["ctrl_time"] += (fs["ctrl_time_seconds"] or 0) * weight
            weighted["knockdowns"] += (fs["knockdowns"] or 0) * weight
            weighted["total_str_landed"] += (fs["total_str_landed"] or 0) * weight

    if total_weight == 0:
        return None

    n = total_weight
    total_wins = weighted["wins"]

    result = {
        "num_fights": len(fights),
        "weighted_win_rate": weighted["wins"] / n if n else 0,
        "sig_str_landed_avg": weighted["sig_str_landed"] / n,
        "sig_str_accuracy": (
            weighted["sig_str_landed"] / weighted["sig_str_attempted"]
            if weighted["sig_str_attempted"] > 0 else 0
        ),
        "td_landed_avg": weighted["td_landed"] / n,
        "td_accuracy": (
            weighted["td_landed"] / weighted["td_attempted"]
            if weighted["td_attempted"] > 0 else 0
        ),
        "sub_att_avg": weighted["sub_att"] / n,
        "ctrl_time_avg": weighted["ctrl_time"] / n,
        "knockdowns_avg": weighted["knockdowns"] / n,
        "ko_rate": weighted["ko_wins"] / total_wins if total_wins > 0 else 0,
        "sub_rate": weighted["sub_wins"] / total_wins if total_wins > 0 else 0,
        "dec_rate": weighted["dec_wins"] / total_wins if total_wins > 0 else 0,
        "finish_rate": (
            (weighted["ko_wins"] + weighted["sub_wins"]) / total_wins
            if total_wins > 0 else 0
        ),
        "avg_rounds": weighted["rounds_fought"] / n if n else 0,
    }
    return result


def classify_style(stats):
    """Classify a fighter's primary style based on their stats.

    Returns: 'striker', 'grappler', 'wrestler', 'balanced'
    """
    if not stats:
        return "unknown"

    striking_score = (
        stats.get("sig_str_landed_avg", 0) * 0.4 +
        stats.get("knockdowns_avg", 0) * 30 +
        stats.get("ko_rate", 0) * 20
    )
    grappling_score = (
        stats.get("td_landed_avg", 0) * 10 +
        stats.get("sub_att_avg", 0) * 15 +
        stats.get("ctrl_time_avg", 0) / 30 +
        stats.get("sub_rate", 0) * 20
    )

    total = striking_score + grappling_score
    if total == 0:
        return "unknown"

    strike_pct = striking_score / total

    if strike_pct > 0.65:
        return "striker"
    elif strike_pct < 0.35:
        return "grappler"
    elif stats.get("td_landed_avg", 0) > 2 and strike_pct < 0.55:
        return "wrestler"
    else:
        return "balanced"


def style_matchup_score(style1, style2):
    """Score how advantageous fighter1's style is vs fighter2.

    Returns a modifier from -0.1 to +0.1.
    Positive = style advantage for fighter1.
    """
    # Classic MMA style triangle
    matchups = {
        ("striker", "grappler"): -0.05,   # grappler can close distance
        ("striker", "wrestler"): -0.03,    # wrestler can control
        ("striker", "striker"): 0.0,
        ("striker", "balanced"): 0.0,
        ("grappler", "striker"): 0.05,
        ("grappler", "wrestler"): 0.03,    # similar ground skills
        ("grappler", "grappler"): 0.0,
        ("grappler", "balanced"): 0.02,
        ("wrestler", "striker"): 0.03,
        ("wrestler", "grappler"): -0.03,
        ("wrestler", "wrestler"): 0.0,
        ("wrestler", "balanced"): 0.01,
        ("balanced", "striker"): 0.0,
        ("balanced", "grappler"): -0.02,
        ("balanced", "wrestler"): -0.01,
        ("balanced", "balanced"): 0.0,
    }
    return matchups.get((style1, style2), 0.0)


def stance_matchup_score(stance1, stance2):
    """Score stance matchup advantage. Southpaw vs orthodox is notable."""
    if not stance1 or not stance2:
        return 0.0
    s1 = stance1.lower().strip()
    s2 = stance2.lower().strip()
    if s1 == "southpaw" and s2 == "orthodox":
        return 0.02  # slight southpaw advantage
    elif s1 == "orthodox" and s2 == "southpaw":
        return -0.01
    return 0.0


def reach_advantage_score(reach1, reach2):
    """Score based on reach differential in inches."""
    if reach1 is None or reach2 is None:
        return 0.0
    diff = reach1 - reach2
    # cap effect at ~5 inches
    return max(-0.05, min(0.05, diff * 0.008))


def age_factor(age):
    """Return a multiplier based on fighter age.

    Prime MMA age is roughly 28-32. Falls off after 35.
    """
    if age is None:
        return 1.0
    if age < 24:
        return 0.95  # inexperience
    elif age <= 32:
        return 1.0   # prime
    elif age <= 35:
        return 0.97
    elif age <= 37:
        return 0.93
    elif age <= 39:
        return 0.88
    else:
        return 0.82


def inactivity_factor(days_off):
    """Return a multiplier based on ring rust / time off."""
    if days_off is None:
        return 1.0
    if days_off < 180:
        return 1.0
    elif days_off < 365:
        return 0.98
    elif days_off < 540:
        return 0.95
    elif days_off < 730:
        return 0.92
    else:
        return 0.88


def favorite_underdog_record(conn, fighter_id):
    """Analyze how a fighter performs as favorite vs underdog.

    Returns dict with win rates as fav / underdog (based on odds history).
    """
    # Check historical odds for this fighter
    rows = conn.execute(
        """SELECT o.fighter1_name, o.fighter2_name, o.fighter1_ml, o.fighter2_ml,
                  f.winner_id, f.fighter1_id, f.fighter2_id
           FROM odds o
           JOIN fights f ON o.fight_id = f.id
           WHERE f.fighter1_id = ? OR f.fighter2_id = ?""",
        (fighter_id, fighter_id)
    ).fetchall()

    if not rows:
        return {"as_fav_wins": 0, "as_fav_total": 0,
                "as_dog_wins": 0, "as_dog_total": 0}

    fav_wins, fav_total = 0, 0
    dog_wins, dog_total = 0, 0

    for row in rows:
        is_f1 = row["fighter1_id"] == fighter_id
        ml = row["fighter1_ml"] if is_f1 else row["fighter2_ml"]
        won = row["winner_id"] == fighter_id

        if ml is not None:
            if ml < 0:  # favorite
                fav_total += 1
                if won:
                    fav_wins += 1
            else:  # underdog
                dog_total += 1
                if won:
                    dog_wins += 1

    return {
        "as_fav_wins": fav_wins,
        "as_fav_total": fav_total,
        "as_fav_rate": fav_wins / fav_total if fav_total else None,
        "as_dog_wins": dog_wins,
        "as_dog_total": dog_total,
        "as_dog_rate": dog_wins / dog_total if dog_total else None,
    }


def build_fighter_profile(conn, fighter_id):
    """Build a comprehensive fighter profile dict."""
    fighter = conn.execute(
        "SELECT * FROM fighters WHERE id = ?", (fighter_id,)
    ).fetchone()
    if not fighter:
        return None

    profile = dict(fighter)
    profile["age"] = calculate_age(fighter["dob"])
    profile["days_off"] = days_since_last_fight(conn, fighter_id)
    profile["weighted_stats"] = compute_weighted_stats(conn, fighter_id)
    profile["style"] = classify_style(profile["weighted_stats"])
    profile["age_multiplier"] = age_factor(profile["age"])
    profile["inactivity_multiplier"] = inactivity_factor(profile["days_off"])
    profile["fav_dog_record"] = favorite_underdog_record(conn, fighter_id)

    # Career stats from fighter_stats table
    career = conn.execute(
        "SELECT * FROM fighter_stats WHERE fighter_id = ? ORDER BY updated_at DESC LIMIT 1",
        (fighter_id,)
    ).fetchone()
    if career:
        profile["career_stats"] = dict(career)
    else:
        profile["career_stats"] = {}

    return profile
