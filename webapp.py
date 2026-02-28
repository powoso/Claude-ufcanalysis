#!/usr/bin/env python3
"""UFC Betting Analyzer — Flask Web Application.

Usage:
    python webapp.py              # Start on port 8080
    PORT=9000 python webapp.py    # Start on custom port
"""

import os
import sys
import json
import socket
import logging
from datetime import datetime

# Ensure project root is on path regardless of where we run from
_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

from flask import Flask, render_template, jsonify, request, redirect, url_for

import config
from database import init_db, get_db, get_upcoming_events, get_completed_events, get_event_fights, get_fighter_by_name, get_fighter_fights
from models.elo import compute_all_elo, get_fighter_elo, elo_win_probability
from models.predictor import predict_fight, backtest, load_model, train_model
from analysis.fighter_profile import build_fighter_profile, classify_style, compute_weighted_stats, calculate_age
from analysis.edge_detection import find_edges, kelly_criterion, calculate_edge, classify_confidence
from analysis.prop_bets import analyze_props, method_of_victory_probs, over_under_analysis
from scrapers.odds_api import fetch_odds, american_to_implied, implied_to_american, get_best_odds

_TEMPLATE_DIR = os.path.join(_ROOT, "web", "templates")
_STATIC_DIR = os.path.join(_ROOT, "web", "static")

app = Flask(__name__, template_folder=_TEMPLATE_DIR, static_folder=_STATIC_DIR)
app.secret_key = os.environ.get("SECRET_KEY", "ufc-analyzer-dev-key")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def fmt_pct(val):
    if val is None:
        return "N/A"
    return f"{val * 100:.1f}%"

def fmt_odds(val):
    if val is None:
        return "N/A"
    v = int(round(val))
    return f"+{v}" if v > 0 else str(v)

def fmt_record(fighter):
    try:
        w = fighter["record_wins"] or 0
        l = fighter["record_losses"] or 0
        d = fighter["record_draws"] or 0
    except (KeyError, TypeError):
        w = l = d = 0
    return f"{w}-{l}-{d}"

def fmt_height(inches):
    if not inches:
        return "N/A"
    ft = int(inches) // 12
    rem = int(inches) % 12
    return f"{ft}'{rem}\""

app.jinja_env.globals.update(
    fmt_pct=fmt_pct, fmt_odds=fmt_odds, fmt_record=fmt_record,
    fmt_height=fmt_height, int=int, abs=abs, round=round, max=max, min=min,
    enumerate=enumerate, len=len,
)


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    """Main dashboard with summary stats and upcoming card."""
    with get_db() as conn:
        upcoming = get_upcoming_events(conn)
        completed = get_completed_events(conn, limit=10)
        fighter_count = conn.execute("SELECT COUNT(*) as c FROM fighters").fetchone()["c"]
        fight_count = conn.execute("SELECT COUNT(*) as c FROM fights").fetchone()["c"]
        event_count = conn.execute("SELECT COUNT(*) as c FROM events").fetchone()["c"]
        prediction_count = conn.execute("SELECT COUNT(*) as c FROM predictions").fetchone()["c"]

        # Model info
        _, metrics = load_model()

        return render_template("dashboard.html",
            upcoming=upcoming,
            completed=completed,
            fighter_count=fighter_count,
            fight_count=fight_count,
            event_count=event_count,
            prediction_count=prediction_count,
            model_metrics=metrics or {},
        )


@app.route("/event/<int:event_id>")
def event_detail(event_id):
    """Detailed fight card with predictions and edges."""
    with get_db() as conn:
        event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if not event:
            return redirect(url_for("dashboard"))

        fights = get_event_fights(conn, event_id)
        pipeline, _ = load_model()
        odds_data = fetch_odds()

        analyses = []
        for fight in fights:
            f1_id = fight["fighter1_id"]
            f2_id = fight["fighter2_id"]

            pred = predict_fight(conn, f1_id, f2_id, pipeline)
            p1 = build_fighter_profile(conn, f1_id)
            p2 = build_fighter_profile(conn, f2_id)
            elo1 = get_fighter_elo(conn, f1_id)
            elo2 = get_fighter_elo(conn, f2_id)
            props = analyze_props(conn, f1_id, f2_id)

            # Match odds
            fight_best_odds = None
            if odds_data:
                for od in odds_data:
                    o1 = od.get("fighter1", "").lower()
                    o2 = od.get("fighter2", "").lower()
                    fn1 = fight["fighter1_name"].lower()
                    fn2 = fight["fighter2_name"].lower()
                    if (fn1 in o1 or o1 in fn1) and (fn2 in o2 or o2 in fn2):
                        fight_best_odds = get_best_odds(od)
                        break
                    if (fn2 in o1 or o1 in fn2) and (fn1 in o2 or o2 in fn1):
                        fight_best_odds = get_best_odds(od)
                        break

            edge_info = None
            if pred and fight_best_odds and fight_best_odds.get("f1_implied"):
                e1 = calculate_edge(pred["fighter1_win_prob"], fight_best_odds["f1_implied"])
                e2 = calculate_edge(pred["fighter2_win_prob"], fight_best_odds["f2_implied"])
                k1 = kelly_criterion(pred["fighter1_win_prob"], fight_best_odds.get("best_f1_ml"))
                k2 = kelly_criterion(pred["fighter2_win_prob"], fight_best_odds.get("best_f2_ml"))
                edge_info = {
                    "f1_edge": e1, "f2_edge": e2,
                    "f1_kelly": k1 * config.BANKROLL, "f2_kelly": k2 * config.BANKROLL,
                    "f1_conf": classify_confidence(e1, k1),
                    "f2_conf": classify_confidence(e2, k2),
                }

            analyses.append({
                "fight": dict(fight),
                "prediction": pred,
                "profile1": p1,
                "profile2": p2,
                "elo1": elo1,
                "elo2": elo2,
                "props": props,
                "odds": fight_best_odds,
                "edge": edge_info,
            })

        return render_template("event.html",
            event=dict(event),
            analyses=analyses,
        )


@app.route("/fighter/<int:fighter_id>")
def fighter_detail(fighter_id):
    """Individual fighter profile page."""
    with get_db() as conn:
        fighter = conn.execute("SELECT * FROM fighters WHERE id = ?", (fighter_id,)).fetchone()
        if not fighter:
            return redirect(url_for("dashboard"))

        profile = build_fighter_profile(conn, fighter_id)
        elo = get_fighter_elo(conn, fighter_id)
        fights = get_fighter_fights(conn, fighter_id, limit=15)

        # Enrich fights with opponent info
        fight_history = []
        for f in fights:
            opp_id = f["fighter2_id"] if f["fighter1_id"] == fighter_id else f["fighter1_id"]
            opp = conn.execute("SELECT name FROM fighters WHERE id = ?", (opp_id,)).fetchone()
            won = f["winner_id"] == fighter_id
            fight_history.append({
                **dict(f),
                "opponent_name": opp["name"] if opp else "Unknown",
                "opponent_id": opp_id,
                "won": won,
                "result": "W" if won else ("L" if f["winner_id"] else "NC"),
            })

        return render_template("fighter.html",
            fighter=dict(fighter),
            profile=profile or {},
            elo=elo,
            fight_history=fight_history,
        )


@app.route("/fighters")
def fighters_list():
    """Browse all fighters."""
    with get_db() as conn:
        search = request.args.get("q", "").strip()
        page = int(request.args.get("page", 1))
        per_page = 50

        if search:
            fighters = conn.execute(
                """SELECT f.*, e.overall as elo
                   FROM fighters f
                   LEFT JOIN elo_ratings e ON f.id = e.fighter_id
                   WHERE LOWER(f.name) LIKE LOWER(?)
                   ORDER BY e.overall DESC NULLS LAST
                   LIMIT ? OFFSET ?""",
                (f"%{search}%", per_page, (page - 1) * per_page)
            ).fetchall()
        else:
            fighters = conn.execute(
                """SELECT f.*, e.overall as elo
                   FROM fighters f
                   LEFT JOIN elo_ratings e ON f.id = e.fighter_id
                   ORDER BY e.overall DESC NULLS LAST
                   LIMIT ? OFFSET ?""",
                (per_page, (page - 1) * per_page)
            ).fetchall()

        total = conn.execute("SELECT COUNT(*) as c FROM fighters").fetchone()["c"]

        return render_template("fighters.html",
            fighters=[dict(f) for f in fighters],
            search=search,
            page=page,
            per_page=per_page,
            total=total,
        )


@app.route("/edges")
def edges_page():
    """Dedicated edge finder page with current edges."""
    with get_db() as conn:
        pipeline, metrics = load_model()
        odds_data = fetch_odds()
        edges = find_edges(conn, odds_data, pipeline) if odds_data else []

        return render_template("edges.html",
            edges=edges,
            model_metrics=metrics or {},
            has_odds=bool(odds_data),
            bankroll=config.BANKROLL,
            kelly_fraction=config.KELLY_FRACTION,
            edge_threshold=config.EDGE_THRESHOLD,
        )


@app.route("/backtest")
def backtest_page():
    """Model backtest results page."""
    with get_db() as conn:
        start = request.args.get("from", "2023-01-01")
        results = backtest(conn, start_date=start)

        # Group results by event for display
        events_map = {}
        for r in results.get("results", []):
            ev = r.get("event", "Unknown")
            if ev not in events_map:
                events_map[ev] = {"name": ev, "date": r.get("date"), "fights": [], "correct": 0, "total": 0}
            events_map[ev]["fights"].append(r)
            events_map[ev]["total"] += 1
            if r.get("correct"):
                events_map[ev]["correct"] += 1

        events_list = sorted(events_map.values(), key=lambda x: x.get("date") or "", reverse=True)

        return render_template("backtest.html",
            results=results,
            events=events_list,
            start_date=start,
        )


@app.route("/api/fighter/<int:fighter_id>/elo")
def api_fighter_elo(fighter_id):
    """JSON API for fighter ELO data (for charts)."""
    with get_db() as conn:
        elo = get_fighter_elo(conn, fighter_id)
        return jsonify(elo)


@app.route("/api/odds/refresh")
def api_refresh_odds():
    """Trigger odds refresh."""
    odds = fetch_odds()
    return jsonify({"count": len(odds), "status": "ok"})


# ── Main ─────────────────────────────────────────────────────────────────────

def create_app():
    init_db()
    return app


def _port_available(port):
    """Check if a TCP port is available."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _find_open_port(preferred):
    """Return preferred port if open, otherwise try alternatives."""
    if _port_available(preferred):
        return preferred
    # On macOS, port 5000 is often used by AirPlay Receiver.
    # Try a few common alternatives.
    for candidate in [8080, 8888, 3000, 9000, 5050]:
        if candidate != preferred and _port_available(candidate):
            return candidate
    return preferred  # fall back and let Flask report the error


if __name__ == "__main__":
    init_db()
    preferred_port = int(os.environ.get("PORT", 8080))
    port = _find_open_port(preferred_port)
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"

    if port != preferred_port:
        print(f"Port {preferred_port} is in use, using {port} instead.")
    print(f"\n  UFC Betting Analyzer")
    print(f"  Running at: http://localhost:{port}\n")

    app.run(host="0.0.0.0", port=port, debug=debug)
