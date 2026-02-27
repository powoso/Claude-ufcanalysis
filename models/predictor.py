"""Logistic regression predictor for UFC fight outcomes."""

import logging
import math
import pickle
import os
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_score
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

import config
from models.elo import get_fighter_elo, elo_win_probability
from analysis.fighter_profile import (
    build_fighter_profile, classify_style, style_matchup_score,
    stance_matchup_score, reach_advantage_score,
    age_factor, inactivity_factor, compute_weighted_stats,
)

logger = logging.getLogger(__name__)
MODEL_PATH = os.path.join(os.path.dirname(__file__), "fight_model.pkl")


def extract_features(conn, fighter1_id, fighter2_id):
    """Extract feature vector for a fight between two fighters.

    Returns a dict of features (or None if data is insufficient).
    """
    p1 = build_fighter_profile(conn, fighter1_id)
    p2 = build_fighter_profile(conn, fighter2_id)
    if not p1 or not p2:
        return None

    s1 = p1.get("weighted_stats") or {}
    s2 = p2.get("weighted_stats") or {}
    if not s1 or not s2:
        return None

    elo1 = get_fighter_elo(conn, fighter1_id)
    elo2 = get_fighter_elo(conn, fighter2_id)

    style1 = classify_style(s1)
    style2 = classify_style(s2)

    features = {
        # ELO differentials
        "elo_overall_diff": elo1["overall"] - elo2["overall"],
        "elo_striking_diff": elo1["striking"] - elo2["striking"],
        "elo_grappling_diff": elo1["grappling"] - elo2["grappling"],
        "elo_cardio_diff": elo1["cardio"] - elo2["cardio"],

        # Striking differentials
        "sig_str_landed_diff": s1.get("sig_str_landed_avg", 0) - s2.get("sig_str_landed_avg", 0),
        "sig_str_acc_diff": s1.get("sig_str_accuracy", 0) - s2.get("sig_str_accuracy", 0),
        "knockdowns_diff": s1.get("knockdowns_avg", 0) - s2.get("knockdowns_avg", 0),

        # Grappling differentials
        "td_landed_diff": s1.get("td_landed_avg", 0) - s2.get("td_landed_avg", 0),
        "td_acc_diff": s1.get("td_accuracy", 0) - s2.get("td_accuracy", 0),
        "sub_att_diff": s1.get("sub_att_avg", 0) - s2.get("sub_att_avg", 0),
        "ctrl_time_diff": s1.get("ctrl_time_avg", 0) - s2.get("ctrl_time_avg", 0),

        # Win rate differential
        "win_rate_diff": s1.get("weighted_win_rate", 0) - s2.get("weighted_win_rate", 0),
        "finish_rate_diff": s1.get("finish_rate", 0) - s2.get("finish_rate", 0),

        # Physical attributes
        "reach_diff": (p1.get("reach_inches") or 0) - (p2.get("reach_inches") or 0),
        "height_diff": (p1.get("height_inches") or 0) - (p2.get("height_inches") or 0),

        # Style & stance
        "style_matchup": style_matchup_score(style1, style2),
        "stance_matchup": stance_matchup_score(
            p1.get("stance"), p2.get("stance")
        ),

        # Age & activity
        "age_factor_diff": age_factor(p1.get("age")) - age_factor(p2.get("age")),
        "inactivity_diff": (
            inactivity_factor(p1.get("days_off")) -
            inactivity_factor(p2.get("days_off"))
        ),

        # Experience
        "fights_diff": s1.get("num_fights", 0) - s2.get("num_fights", 0),
    }

    return features


def build_training_data(conn, min_date="2020-01-01"):
    """Build training dataset from historical fights.

    Returns (X DataFrame, y Series) where y=1 if fighter1 won.
    """
    fights = conn.execute(
        """SELECT f.*, e.date as event_date
           FROM fights f
           JOIN events e ON f.event_id = e.id
           WHERE f.winner_id IS NOT NULL
                 AND e.date IS NOT NULL
                 AND e.date >= ?
           ORDER BY e.date ASC""",
        (min_date,)
    ).fetchall()

    rows = []
    labels = []

    for fight in fights:
        feats = extract_features(conn, fight["fighter1_id"], fight["fighter2_id"])
        if feats is None:
            continue
        rows.append(feats)
        labels.append(1 if fight["winner_id"] == fight["fighter1_id"] else 0)

    if not rows:
        logger.warning("No training data available")
        return None, None

    X = pd.DataFrame(rows)
    y = pd.Series(labels)

    logger.info("Built training set: %d fights, %d features", len(X), len(X.columns))
    return X, y


def train_model(conn, min_date="2020-01-01"):
    """Train the logistic regression model and return it + metrics."""
    X, y = build_training_data(conn, min_date)
    if X is None or len(X) < 20:
        logger.warning("Insufficient training data (%s samples)", len(X) if X is not None else 0)
        return None, {}

    pipeline = Pipeline([
        ("scaler", StandardScaler()),
        ("lr", LogisticRegression(
            C=1.0,
            max_iter=1000,
            class_weight="balanced",
            random_state=42,
        )),
    ])

    # Cross-validation
    cv_scores = cross_val_score(pipeline, X, y, cv=min(5, len(X) // 5 or 2), scoring="accuracy")
    logger.info("CV accuracy: %.3f (+/- %.3f)", cv_scores.mean(), cv_scores.std())

    # Train on full data
    pipeline.fit(X, y)

    # Feature importances
    lr = pipeline.named_steps["lr"]
    feature_importance = dict(zip(X.columns, lr.coef_[0]))

    metrics = {
        "cv_accuracy": float(cv_scores.mean()),
        "cv_std": float(cv_scores.std()),
        "n_samples": len(X),
        "n_features": len(X.columns),
        "feature_importance": feature_importance,
    }

    # Save model
    with open(MODEL_PATH, "wb") as f:
        pickle.dump({"pipeline": pipeline, "features": list(X.columns), "metrics": metrics}, f)
    logger.info("Model saved to %s", MODEL_PATH)

    return pipeline, metrics


def load_model():
    """Load a previously trained model."""
    if not os.path.exists(MODEL_PATH):
        return None, None
    with open(MODEL_PATH, "rb") as f:
        data = pickle.load(f)
    return data["pipeline"], data.get("metrics", {})


def predict_fight(conn, fighter1_id, fighter2_id, pipeline=None):
    """Predict win probability for fighter1.

    Uses the trained logistic regression model if available,
    falls back to ELO-based prediction.
    """
    feats = extract_features(conn, fighter1_id, fighter2_id)

    # ELO baseline prediction
    elo1 = get_fighter_elo(conn, fighter1_id)
    elo2 = get_fighter_elo(conn, fighter2_id)
    elo_prob = elo_win_probability(elo1["overall"], elo2["overall"])

    if pipeline is None:
        pipeline, _ = load_model()

    if pipeline is not None and feats is not None:
        X = pd.DataFrame([feats])
        try:
            model_prob = pipeline.predict_proba(X)[0][1]  # P(fighter1 wins)
        except Exception as e:
            logger.warning("Model prediction failed: %s", e)
            model_prob = elo_prob
    else:
        model_prob = elo_prob

    # Blend: 60% model, 40% ELO if model available
    if pipeline is not None and feats is not None:
        blended = 0.6 * model_prob + 0.4 * elo_prob
    else:
        blended = elo_prob

    return {
        "fighter1_win_prob": blended,
        "fighter2_win_prob": 1.0 - blended,
        "model_prob": model_prob,
        "elo_prob": elo_prob,
        "elo1": elo1,
        "elo2": elo2,
        "features": feats,
    }


def backtest(conn, start_date="2023-01-01"):
    """Backtest the model against historical results.

    Walk-forward: for each fight, predict using only prior data,
    then compare to actual result.
    """
    fights = conn.execute(
        """SELECT f.*, e.date as event_date, e.name as event_name
           FROM fights f
           JOIN events e ON f.event_id = e.id
           WHERE f.winner_id IS NOT NULL
                 AND e.date IS NOT NULL
                 AND e.date >= ?
           ORDER BY e.date ASC""",
        (start_date,)
    ).fetchall()

    results = []
    correct = 0
    total = 0
    total_log_loss = 0

    for fight in fights:
        pred = predict_fight(conn, fight["fighter1_id"], fight["fighter2_id"])
        if pred is None:
            continue

        p1_win = pred["fighter1_win_prob"]
        actual_winner = fight["winner_id"]
        predicted_winner = fight["fighter1_id"] if p1_win >= 0.5 else fight["fighter2_id"]
        was_correct = predicted_winner == actual_winner

        if was_correct:
            correct += 1
        total += 1

        # Log loss
        actual_label = 1 if actual_winner == fight["fighter1_id"] else 0
        p_clipped = max(0.01, min(0.99, p1_win))
        ll = -(actual_label * math.log(p_clipped) + (1 - actual_label) * math.log(1 - p_clipped))
        total_log_loss += ll

        results.append({
            "fight_id": fight["id"],
            "event": fight["event_name"],
            "date": fight["event_date"],
            "fighter1_id": fight["fighter1_id"],
            "fighter2_id": fight["fighter2_id"],
            "predicted_prob": p1_win,
            "predicted_winner_id": predicted_winner,
            "actual_winner_id": actual_winner,
            "correct": was_correct,
        })

    accuracy = correct / total if total else 0
    avg_log_loss = total_log_loss / total if total else 0

    logger.info("Backtest: %d/%d correct (%.1f%%), avg log loss: %.4f",
                correct, total, accuracy * 100, avg_log_loss)

    return {
        "accuracy": accuracy,
        "correct": correct,
        "total": total,
        "avg_log_loss": avg_log_loss,
        "results": results,
    }
