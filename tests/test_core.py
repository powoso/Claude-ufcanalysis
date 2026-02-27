"""Smoke tests for core modules — uses synthetic data, no network required."""

import os
import sys
import sqlite3
import tempfile
import unittest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config

# Use a temp database for testing
_test_db = tempfile.mktemp(suffix=".db")
config.DB_PATH = _test_db

import database
from models.elo import compute_all_elo, get_fighter_elo, elo_win_probability, expected_score
from analysis.fighter_profile import (
    calculate_age, age_factor, inactivity_factor, classify_style,
    style_matchup_score, stance_matchup_score, reach_advantage_score,
)
from analysis.edge_detection import kelly_criterion, calculate_edge, classify_confidence
from analysis.prop_bets import method_of_victory_probs, over_under_analysis
from scrapers.odds_api import american_to_implied, implied_to_american


class TestOddsConversion(unittest.TestCase):
    def test_favorite(self):
        prob = american_to_implied(-200)
        self.assertAlmostEqual(prob, 2/3, places=3)

    def test_underdog(self):
        prob = american_to_implied(200)
        self.assertAlmostEqual(prob, 1/3, places=3)

    def test_even(self):
        prob = american_to_implied(100)
        self.assertAlmostEqual(prob, 0.5, places=3)

    def test_round_trip(self):
        # +100 is excluded: both +100 and -100 map to 50%, so round-trip
        # is ambiguous at the boundary. This is inherent to American odds.
        for odds in [-300, -150, -110, 150, 300]:
            prob = american_to_implied(odds)
            back = implied_to_american(prob)
            self.assertAlmostEqual(odds, back, delta=2)


class TestKellyCriterion(unittest.TestCase):
    def test_positive_ev(self):
        # 60% win probability at +100 odds (even money)
        size = kelly_criterion(0.60, 100)
        self.assertGreater(size, 0)
        # Full Kelly would be 20%, fractional at 25% = 5%
        self.assertAlmostEqual(size, 0.05, places=3)

    def test_negative_ev(self):
        # 30% win probability at even money → negative EV
        size = kelly_criterion(0.30, 100)
        self.assertEqual(size, 0.0)

    def test_big_underdog_edge(self):
        # 40% prob at +300 odds → positive EV
        size = kelly_criterion(0.40, 300)
        self.assertGreater(size, 0)

    def test_none_odds(self):
        self.assertEqual(kelly_criterion(0.5, None), 0.0)


class TestEdge(unittest.TestCase):
    def test_positive_edge(self):
        edge = calculate_edge(0.60, 0.50)
        self.assertAlmostEqual(edge, 0.10, places=3)

    def test_no_edge(self):
        edge = calculate_edge(0.50, 0.55)
        self.assertAlmostEqual(edge, -0.05, places=3)

    def test_confidence_high(self):
        self.assertEqual(classify_confidence(0.16, 0.05), "high")

    def test_confidence_medium(self):
        self.assertEqual(classify_confidence(0.09, 0.03), "medium")

    def test_confidence_low(self):
        self.assertEqual(classify_confidence(0.06, 0.01), "low")

    def test_confidence_none(self):
        self.assertEqual(classify_confidence(0.02, 0.01), "none")


class TestELO(unittest.TestCase):
    def test_expected_score_equal(self):
        self.assertAlmostEqual(expected_score(1500, 1500), 0.5, places=5)

    def test_expected_score_higher(self):
        score = expected_score(1600, 1400)
        self.assertGreater(score, 0.5)
        self.assertLess(score, 1.0)

    def test_win_probability_symmetry(self):
        p1 = elo_win_probability(1600, 1400)
        p2 = elo_win_probability(1400, 1600)
        self.assertAlmostEqual(p1 + p2, 1.0, places=5)


class TestAgeFactor(unittest.TestCase):
    def test_prime(self):
        self.assertEqual(age_factor(30), 1.0)

    def test_old(self):
        self.assertLess(age_factor(40), 0.9)

    def test_young(self):
        self.assertLess(age_factor(22), 1.0)

    def test_none(self):
        self.assertEqual(age_factor(None), 1.0)


class TestInactivity(unittest.TestCase):
    def test_active(self):
        self.assertEqual(inactivity_factor(90), 1.0)

    def test_long_layoff(self):
        self.assertLess(inactivity_factor(800), 0.95)


class TestStyleClassification(unittest.TestCase):
    def test_striker(self):
        stats = {
            "sig_str_landed_avg": 5.0, "knockdowns_avg": 0.3,
            "ko_rate": 0.6, "td_landed_avg": 0.2, "sub_att_avg": 0.1,
            "ctrl_time_avg": 10, "sub_rate": 0.05,
        }
        self.assertEqual(classify_style(stats), "striker")

    def test_grappler(self):
        stats = {
            "sig_str_landed_avg": 1.0, "knockdowns_avg": 0.0,
            "ko_rate": 0.1, "td_landed_avg": 4.0, "sub_att_avg": 3.0,
            "ctrl_time_avg": 300, "sub_rate": 0.6,
        }
        self.assertEqual(classify_style(stats), "grappler")

    def test_none(self):
        self.assertEqual(classify_style(None), "unknown")


class TestDatabaseAndELO(unittest.TestCase):
    def setUp(self):
        database.init_db()

    def test_full_elo_pipeline(self):
        """Test ELO computation with synthetic fight data."""
        with database.get_db() as conn:
            # Create fighters
            f1_id = database.upsert_fighter(conn, "Test Fighter A")
            f2_id = database.upsert_fighter(conn, "Test Fighter B")
            f3_id = database.upsert_fighter(conn, "Test Fighter C")

            # Create an event
            e_id = database.upsert_event(conn, "Test Event 1", date="2024-01-01", is_completed=1)

            # Create fights: A beats B, B beats C
            database.upsert_fight(
                conn, e_id, f1_id, f2_id,
                winner_id=f1_id, method="KO/TKO", round=2
            )
            database.upsert_fight(
                conn, e_id, f2_id, f3_id,
                winner_id=f2_id, method="Decision", round=3
            )
            conn.commit()

            # Compute ELO
            ratings = compute_all_elo(conn)
            conn.commit()

            # A should be rated highest (won by KO)
            self.assertGreater(ratings[f1_id]["overall"], config.ELO_INITIAL)
            # A should have high striking ELO (KO win)
            self.assertGreater(ratings[f1_id]["striking"], config.ELO_INITIAL)
            # B lost to A but beat C
            # C lost, should be below initial
            self.assertLess(ratings[f3_id]["overall"], config.ELO_INITIAL)

            # get_fighter_elo should work
            elo_a = get_fighter_elo(conn, f1_id)
            self.assertEqual(elo_a["overall"], ratings[f1_id]["overall"])

    def tearDown(self):
        try:
            os.unlink(_test_db)
        except OSError:
            pass


class TestReachAdvantage(unittest.TestCase):
    def test_longer_reach(self):
        score = reach_advantage_score(76, 70)
        self.assertGreater(score, 0)

    def test_shorter_reach(self):
        score = reach_advantage_score(68, 74)
        self.assertLess(score, 0)

    def test_equal(self):
        score = reach_advantage_score(72, 72)
        self.assertEqual(score, 0.0)

    def test_none(self):
        score = reach_advantage_score(None, 72)
        self.assertEqual(score, 0.0)


class TestStanceMatchup(unittest.TestCase):
    def test_southpaw_advantage(self):
        score = stance_matchup_score("Southpaw", "Orthodox")
        self.assertGreater(score, 0)

    def test_orthodox_disadvantage(self):
        score = stance_matchup_score("Orthodox", "Southpaw")
        self.assertLess(score, 0)


if __name__ == "__main__":
    unittest.main()
