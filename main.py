#!/usr/bin/env python3
"""UFC Betting Analyzer — main entry point.

Usage:
    python main.py                  # Full pipeline: scrape, model, analyze, report
    python main.py --scrape         # Scrape data only
    python main.py --train          # Train/retrain model only
    python main.py --analyze        # Analyze upcoming card only (uses cached data)
    python main.py --backtest       # Run backtest against historical data
    python main.py --report         # Generate report only (uses existing analysis)
    python main.py --quick          # Quick mode: smaller scrape, faster run
"""

import argparse
import logging
import sys
import os

# Ensure project root is on path regardless of where we run from
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from database import init_db, get_db, get_upcoming_events, get_completed_events
from scrapers.ingest import full_ingest, ingest_odds
from scrapers.odds_api import fetch_odds
from models.elo import compute_all_elo
from models.predictor import train_model, predict_fight, backtest, load_model
from analysis.edge_detection import find_edges, store_predictions
from analysis.prop_bets import analyze_props
from reports.generator import (
    generate_fight_analysis, generate_markdown_report, save_report,
)


def setup_logging(verbose=False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_scrape(args):
    """Run data ingestion pipeline."""
    with get_db() as conn:
        max_pages = 1 if args.quick else 3
        max_fight_details = 30 if args.quick else 100
        max_fighter_details = 50 if args.quick else 200
        sherdog_limit = 10 if args.quick else 50

        full_ingest(
            conn,
            max_event_pages=max_pages,
            max_fight_details=max_fight_details,
            max_fighter_details=max_fighter_details,
            sherdog_limit=sherdog_limit,
        )


def cmd_train(args):
    """Train the prediction model."""
    with get_db() as conn:
        # First compute ELO ratings
        logging.info("Computing ELO ratings...")
        compute_all_elo(conn)
        conn.commit()

        # Train logistic regression
        logging.info("Training prediction model...")
        pipeline, metrics = train_model(conn, min_date="2020-01-01")

        if pipeline:
            print("\n=== Model Training Results ===")
            print(f"  CV Accuracy:  {metrics.get('cv_accuracy', 0)*100:.1f}%")
            print(f"  CV Std Dev:   {metrics.get('cv_std', 0)*100:.1f}%")
            print(f"  Samples:      {metrics.get('n_samples', 0)}")
            print(f"  Features:     {metrics.get('n_features', 0)}")

            fi = metrics.get("feature_importance", {})
            if fi:
                print("\n  Top features by importance:")
                sorted_fi = sorted(fi.items(), key=lambda x: abs(x[1]), reverse=True)
                for name, imp in sorted_fi[:10]:
                    print(f"    {name:30s} {imp:+.4f}")
            print()
        else:
            print("WARNING: Model training failed (insufficient data)")
            print("Run with --scrape first to collect fight data.\n")


def cmd_backtest(args):
    """Run backtest against historical results."""
    with get_db() as conn:
        start = args.backtest_from or "2023-01-01"
        logging.info("Running backtest from %s...", start)
        results = backtest(conn, start_date=start)

        print("\n=== Backtest Results ===")
        print(f"  Period:    {start} to present")
        print(f"  Accuracy:  {results['accuracy']*100:.1f}%")
        print(f"  Record:    {results['correct']}/{results['total']}")
        print(f"  Log Loss:  {results['avg_log_loss']:.4f}")
        print()


def cmd_analyze(args):
    """Analyze upcoming card and find edges."""
    with get_db() as conn:
        # Fetch latest odds
        odds_data = fetch_odds()

        # Load model
        pipeline, metrics = load_model()
        if not pipeline:
            logging.warning("No trained model found. Training now...")
            compute_all_elo(conn)
            conn.commit()
            pipeline, metrics = train_model(conn)

        upcoming = get_upcoming_events(conn)
        if not upcoming:
            print("No upcoming events found. Run --scrape first.")
            return

        for event in upcoming:
            event_name = event["name"]
            event_id = event["id"]
            print(f"\n{'='*60}")
            print(f"Analyzing: {event_name}")
            print(f"Date: {event['date'] or 'TBD'}")
            print(f"{'='*60}\n")

            # Find edges
            edges = find_edges(conn, odds_data, pipeline)

            # Generate detailed fight analysis
            analyses = generate_fight_analysis(conn, event_id, odds_data, pipeline)

            # Backtest for context
            bt = backtest(conn) if not args.quick else None

            # Generate and save report
            report = generate_markdown_report(
                conn, event_name, event_id, analyses, edges, bt
            )
            report_path = save_report(report, event_name)
            print(report)
            print(f"\nReport saved: {report_path}")

            # Store predictions
            if edges:
                store_predictions(conn, edges, event_name)
                conn.commit()


def cmd_report(args):
    """Generate report from existing data."""
    with get_db() as conn:
        pipeline, metrics = load_model()
        odds_data = fetch_odds()

        upcoming = get_upcoming_events(conn)
        if not upcoming:
            # Fall back to most recent completed event
            completed = get_completed_events(conn, limit=1)
            if completed:
                upcoming = [completed[0]]

        if not upcoming:
            print("No events found. Run --scrape first.")
            return

        event = upcoming[0]
        event_name = event["name"]
        event_id = event["id"]

        edges = find_edges(conn, odds_data, pipeline) if odds_data else []
        analyses = generate_fight_analysis(conn, event_id, odds_data, pipeline)
        bt = backtest(conn) if not args.quick else None

        report = generate_markdown_report(
            conn, event_name, event_id, analyses, edges, bt
        )
        report_path = save_report(report, event_name)
        print(report)
        print(f"\nReport saved: {report_path}")


def main():
    parser = argparse.ArgumentParser(
        description="UFC Betting Analyzer — edge finder and prediction system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--scrape", action="store_true",
                        help="Run data ingestion pipeline")
    parser.add_argument("--train", action="store_true",
                        help="Train the prediction model")
    parser.add_argument("--backtest", action="store_true",
                        help="Run backtesting on historical data")
    parser.add_argument("--backtest-from", default="2023-01-01",
                        help="Start date for backtesting (default: 2023-01-01)")
    parser.add_argument("--analyze", action="store_true",
                        help="Analyze upcoming card and find edges")
    parser.add_argument("--report", action="store_true",
                        help="Generate report from existing data")
    parser.add_argument("--quick", action="store_true",
                        help="Quick mode: smaller scrape, skip backtest in reports")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose logging")

    args = parser.parse_args()
    setup_logging(args.verbose)

    # Initialize database
    init_db()

    # If no specific command, run full pipeline
    if not any([args.scrape, args.train, args.backtest, args.analyze, args.report]):
        print("=" * 60)
        print("  UFC Betting Analyzer — Full Pipeline")
        print("=" * 60)
        print()

        print("[1/5] Scraping data...")
        cmd_scrape(args)

        print("\n[2/5] Computing ELO ratings...")
        with get_db() as conn:
            compute_all_elo(conn)
            conn.commit()

        print("\n[3/5] Training model...")
        cmd_train(args)

        print("\n[4/5] Analyzing upcoming card...")
        cmd_analyze(args)

        print("\n[5/5] Done!")
        return

    if args.scrape:
        cmd_scrape(args)
    if args.train:
        cmd_train(args)
    if args.backtest:
        cmd_backtest(args)
    if args.analyze:
        cmd_analyze(args)
    if args.report:
        cmd_report(args)


if __name__ == "__main__":
    main()
