"""Report generator — outputs clean markdown reports."""

import os
import logging
from datetime import datetime

import config
from analysis.edge_detection import find_edges, kelly_criterion
from analysis.prop_bets import analyze_props
from analysis.fighter_profile import build_fighter_profile, classify_style
from models.predictor import predict_fight, backtest
from models.elo import get_fighter_elo
from scrapers.odds_api import implied_to_american, american_to_implied
from database import get_event_fights, get_fighter_by_name

logger = logging.getLogger(__name__)
REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "reports_output")


def _fmt_pct(val):
    if val is None:
        return "N/A"
    return f"{val * 100:.1f}%"


def _fmt_odds(val):
    if val is None:
        return "N/A"
    v = int(round(val))
    return f"+{v}" if v > 0 else str(v)


def _fmt_kelly(val):
    if val is None or val <= 0:
        return "—"
    return f"{val:.2f}u"


def _confidence_badge(conf):
    badges = {"high": "HIGH", "medium": "MED", "low": "LOW", "none": "—"}
    return badges.get(conf, "—")


def generate_fight_analysis(conn, event_id, fight_odds_list=None, pipeline=None):
    """Generate analysis for all fights on an event card."""
    fights = get_event_fights(conn, event_id)
    if not fights:
        return []

    analyses = []
    for fight in fights:
        f1_id = fight["fighter1_id"]
        f2_id = fight["fighter2_id"]
        f1_name = fight["fighter1_name"]
        f2_name = fight["fighter2_name"]

        # Prediction
        pred = predict_fight(conn, f1_id, f2_id, pipeline)

        # Profiles
        p1 = build_fighter_profile(conn, f1_id)
        p2 = build_fighter_profile(conn, f2_id)

        # Props
        fight_odds = _match_fight_odds(f1_name, f2_name, fight_odds_list)
        props = analyze_props(conn, f1_id, f2_id, fight_odds)

        # ELO
        elo1 = get_fighter_elo(conn, f1_id)
        elo2 = get_fighter_elo(conn, f2_id)

        analyses.append({
            "fight": dict(fight),
            "prediction": pred,
            "profile1": p1,
            "profile2": p2,
            "props": props,
            "elo1": elo1,
            "elo2": elo2,
            "fight_odds": fight_odds,
        })

    return analyses


def _match_fight_odds(f1_name, f2_name, odds_list):
    """Try to match a fight to odds data by fighter names."""
    if not odds_list:
        return None
    f1_lower = f1_name.lower().strip()
    f2_lower = f2_name.lower().strip()

    for odds in odds_list:
        o1 = odds.get("fighter1", "").lower().strip()
        o2 = odds.get("fighter2", "").lower().strip()
        # Check both orderings
        if (f1_lower in o1 or o1 in f1_lower) and (f2_lower in o2 or o2 in f2_lower):
            return odds
        if (f2_lower in o1 or o1 in f2_lower) and (f1_lower in o2 or o2 in f1_lower):
            return odds
    return None


def generate_markdown_report(conn, event_name, event_id, fight_analyses, edges,
                             backtest_results=None):
    """Generate a complete markdown report for an event."""
    lines = []
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    lines.append(f"# UFC Betting Analysis: {event_name}")
    lines.append(f"*Generated: {now}*\n")

    # --- Model Accuracy ---
    if backtest_results:
        lines.append("## Model Performance")
        lines.append(f"- **Backtest Accuracy:** {backtest_results['accuracy']*100:.1f}% "
                      f"({backtest_results['correct']}/{backtest_results['total']})")
        lines.append(f"- **Avg Log Loss:** {backtest_results['avg_log_loss']:.4f}")
        lines.append("")

    # --- Top Edges ---
    if edges:
        lines.append("## Ranked Edges by Expected Value\n")
        lines.append("| # | Fighter | vs | Model Prob | Market Implied | Edge | "
                      "Best Odds | Book | Kelly Size | Confidence |")
        lines.append("|---|---------|-----|-----------|---------------|------|"
                      "----------|------|------------|------------|")
        for i, e in enumerate(edges, 1):
            lines.append(
                f"| {i} | **{e['fighter_name']}** | {e['opponent_name']} | "
                f"{_fmt_pct(e['model_prob'])} | {_fmt_pct(e['market_implied'])} | "
                f"{_fmt_pct(e['edge'])} | {_fmt_odds(e['best_odds'])} | "
                f"{e.get('best_book', 'N/A')} | {_fmt_kelly(e['kelly_units'])} | "
                f"{_confidence_badge(e['confidence'])} |"
            )
        lines.append("")
    else:
        lines.append("## Edges\n")
        lines.append("*No +EV edges identified for this card (threshold: "
                      f"{config.EDGE_THRESHOLD*100:.0f}% implied probability).*\n")

    # --- Fight-by-fight analysis ---
    lines.append("## Fight Card Analysis\n")

    for analysis in fight_analyses:
        fight = analysis["fight"]
        pred = analysis["prediction"]
        p1 = analysis.get("profile1") or {}
        p2 = analysis.get("profile2") or {}
        props = analysis.get("props") or {}
        elo1 = analysis.get("elo1") or {}
        elo2 = analysis.get("elo2") or {}

        f1_name = fight["fighter1_name"]
        f2_name = fight["fighter2_name"]
        wc = fight.get("weight_class", "")

        title_tag = " (Title)" if fight.get("is_title_fight") else ""
        main_tag = " — Main Event" if fight.get("is_main_event") else ""

        lines.append(f"### {f1_name} vs {f2_name}{title_tag}{main_tag}")
        lines.append(f"*{wc}*\n")

        if pred:
            lines.append("**Win Probability:**")
            lines.append(f"- {f1_name}: {_fmt_pct(pred.get('fighter1_win_prob'))}")
            lines.append(f"- {f2_name}: {_fmt_pct(pred.get('fighter2_win_prob'))}")
            lines.append("")

        # ELO ratings
        lines.append("**ELO Ratings:**")
        lines.append(f"| | Overall | Striking | Grappling | Cardio |")
        lines.append(f"|--|---------|----------|-----------|--------|")
        lines.append(
            f"| {f1_name} | {elo1.get('overall', 1500):.0f} | "
            f"{elo1.get('striking', 1500):.0f} | "
            f"{elo1.get('grappling', 1500):.0f} | "
            f"{elo1.get('cardio', 1500):.0f} |"
        )
        lines.append(
            f"| {f2_name} | {elo2.get('overall', 1500):.0f} | "
            f"{elo2.get('striking', 1500):.0f} | "
            f"{elo2.get('grappling', 1500):.0f} | "
            f"{elo2.get('cardio', 1500):.0f} |"
        )
        lines.append("")

        # Fighter profiles
        s1 = p1.get("weighted_stats") or {}
        s2 = p2.get("weighted_stats") or {}
        style1 = classify_style(s1) if s1 else "unknown"
        style2 = classify_style(s2) if s2 else "unknown"

        lines.append("**Matchup:**")
        lines.append(f"- Styles: {style1.title()} vs {style2.title()}")

        if p1.get("reach_inches") and p2.get("reach_inches"):
            reach_diff = p1["reach_inches"] - p2["reach_inches"]
            adv = f1_name if reach_diff > 0 else f2_name
            lines.append(f"- Reach advantage: {adv} (+{abs(reach_diff):.0f}\")")

        if p1.get("age") or p2.get("age"):
            lines.append(f"- Ages: {p1.get('age', '?')} vs {p2.get('age', '?')}")

        if p1.get("days_off") or p2.get("days_off"):
            lines.append(
                f"- Days since last fight: {p1.get('days_off', '?')} vs {p2.get('days_off', '?')}"
            )
        lines.append("")

        # Props
        mov = props.get("method_of_victory")
        if mov:
            lines.append("**Method of Victory Probabilities:**")
            lines.append(f"- KO/TKO: {_fmt_pct(mov.get('ko_prob'))}")
            lines.append(f"- Submission: {_fmt_pct(mov.get('sub_prob'))}")
            lines.append(f"- Decision: {_fmt_pct(mov.get('dec_prob'))}")
            lines.append("")

        ou = props.get("over_under")
        if ou:
            lines.append("**Over/Under Analysis:**")
            lines.append(
                f"- Expected fight length: {ou['expected_rounds']:.1f} rounds"
            )
            lines.append(f"- Over {ou['posted_total']}: {_fmt_pct(ou['over_prob'])}")
            lines.append(f"- Under {ou['posted_total']}: {_fmt_pct(ou['under_prob'])}")
            lines.append("")

        prop_edges = props.get("prop_edges", [])
        if prop_edges:
            lines.append("**Prop Bet Edges:**")
            for pe in prop_edges:
                lines.append(
                    f"- {pe['value']}: Model {_fmt_pct(pe['model_prob'])} vs "
                    f"Market {_fmt_pct(pe['market_implied'])} "
                    f"(Edge: {_fmt_pct(pe['edge'])})"
                )
            lines.append("")

        lines.append("---\n")

    # --- Summary ---
    lines.append("## Betting Summary\n")
    if edges:
        total_units = sum(e["kelly_units"] for e in edges)
        lines.append(f"- **Total edges found:** {len(edges)}")
        lines.append(f"- **Total suggested allocation:** {total_units:.2f} units "
                      f"(of {config.BANKROLL} unit bankroll)")
        lines.append(f"- **Kelly fraction:** {config.KELLY_FRACTION*100:.0f}%")
        lines.append(f"- **Edge threshold:** {config.EDGE_THRESHOLD*100:.0f}%")
    else:
        lines.append("No actionable edges found. Consider passing on this card.")
    lines.append("")

    lines.append("---")
    lines.append("*Disclaimer: This analysis is for informational and entertainment "
                  "purposes only. Past performance does not guarantee future results. "
                  "Always gamble responsibly.*")

    return "\n".join(lines)


def save_report(markdown_text, event_name):
    """Save report to file and return path."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in event_name)
    date_str = datetime.utcnow().strftime("%Y%m%d")
    filename = f"{date_str}_{safe_name}.md"
    path = os.path.join(REPORTS_DIR, filename)
    with open(path, "w") as f:
        f.write(markdown_text)
    logger.info("Report saved: %s", path)
    return path
