"""SQLite database layer for UFC Betting Analyzer."""

import sqlite3
import json
from datetime import datetime
from contextlib import contextmanager

import config


def _dict_factory(cursor, row):
    """Row factory that returns dicts (supports .get(), iteration, etc.)."""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def get_connection():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = _dict_factory
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create all tables if they don't exist."""
    with get_db() as conn:
        conn.executescript(SCHEMA)
    return True


SCHEMA = """
CREATE TABLE IF NOT EXISTS fighters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    nickname TEXT,
    height_inches REAL,
    reach_inches REAL,
    stance TEXT,
    dob TEXT,
    record_wins INTEGER DEFAULT 0,
    record_losses INTEGER DEFAULT 0,
    record_draws INTEGER DEFAULT 0,
    record_nc INTEGER DEFAULT 0,
    weight_class TEXT,
    team TEXT,
    ufcstats_url TEXT UNIQUE,
    sherdog_url TEXT,
    updated_at TEXT,
    UNIQUE(name, dob)
);

CREATE TABLE IF NOT EXISTS fighter_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fighter_id INTEGER NOT NULL,
    slpm REAL,              -- significant strikes landed per minute
    str_acc REAL,           -- striking accuracy %
    sapm REAL,              -- significant strikes absorbed per minute
    str_def REAL,           -- striking defense %
    td_avg REAL,            -- takedowns per 15 min
    td_acc REAL,            -- takedown accuracy %
    td_def REAL,            -- takedown defense %
    sub_avg REAL,           -- submission attempts per 15 min
    ctrl_time_avg REAL,     -- avg control time seconds per fight
    ko_rate REAL,           -- KO/TKO finish rate
    sub_rate REAL,          -- submission finish rate
    dec_rate REAL,          -- decision rate
    finish_rate REAL,       -- overall finish rate
    avg_fight_time REAL,    -- average fight time in seconds
    win_streak INTEGER DEFAULT 0,
    lose_streak INTEGER DEFAULT 0,
    updated_at TEXT,
    FOREIGN KEY (fighter_id) REFERENCES fighters(id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    date TEXT,
    location TEXT,
    is_completed INTEGER DEFAULT 0,
    ufcstats_url TEXT UNIQUE,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS fights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    fighter1_id INTEGER NOT NULL,
    fighter2_id INTEGER NOT NULL,
    winner_id INTEGER,
    weight_class TEXT,
    method TEXT,
    round INTEGER,
    time TEXT,
    is_main_event INTEGER DEFAULT 0,
    is_title_fight INTEGER DEFAULT 0,
    ufcstats_url TEXT UNIQUE,
    updated_at TEXT,
    FOREIGN KEY (event_id) REFERENCES events(id),
    FOREIGN KEY (fighter1_id) REFERENCES fighters(id),
    FOREIGN KEY (fighter2_id) REFERENCES fighters(id)
);

CREATE TABLE IF NOT EXISTS fight_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fight_id INTEGER NOT NULL,
    fighter_id INTEGER NOT NULL,
    knockdowns INTEGER DEFAULT 0,
    sig_str_landed INTEGER DEFAULT 0,
    sig_str_attempted INTEGER DEFAULT 0,
    total_str_landed INTEGER DEFAULT 0,
    total_str_attempted INTEGER DEFAULT 0,
    td_landed INTEGER DEFAULT 0,
    td_attempted INTEGER DEFAULT 0,
    sub_att INTEGER DEFAULT 0,
    rev INTEGER DEFAULT 0,
    ctrl_time_seconds INTEGER DEFAULT 0,
    head_landed INTEGER DEFAULT 0,
    head_attempted INTEGER DEFAULT 0,
    body_landed INTEGER DEFAULT 0,
    body_attempted INTEGER DEFAULT 0,
    leg_landed INTEGER DEFAULT 0,
    leg_attempted INTEGER DEFAULT 0,
    FOREIGN KEY (fight_id) REFERENCES fights(id),
    FOREIGN KEY (fighter_id) REFERENCES fighters(id),
    UNIQUE(fight_id, fighter_id)
);

CREATE TABLE IF NOT EXISTS elo_ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fighter_id INTEGER NOT NULL,
    overall REAL DEFAULT 1500,
    striking REAL DEFAULT 1500,
    grappling REAL DEFAULT 1500,
    cardio REAL DEFAULT 1500,
    as_of_fight_id INTEGER,
    updated_at TEXT,
    FOREIGN KEY (fighter_id) REFERENCES fighters(id),
    FOREIGN KEY (as_of_fight_id) REFERENCES fights(id)
);

CREATE TABLE IF NOT EXISTS odds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fight_id INTEGER,
    fighter1_name TEXT,
    fighter2_name TEXT,
    sportsbook TEXT,
    fighter1_ml INTEGER,
    fighter2_ml INTEGER,
    over_under_rounds REAL,
    over_price INTEGER,
    under_price INTEGER,
    fetched_at TEXT,
    FOREIGN KEY (fight_id) REFERENCES fights(id)
);

CREATE TABLE IF NOT EXISTS odds_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fight_id INTEGER,
    fighter1_name TEXT,
    fighter2_name TEXT,
    sportsbook TEXT,
    fighter1_ml INTEGER,
    fighter2_ml INTEGER,
    recorded_at TEXT,
    FOREIGN KEY (fight_id) REFERENCES fights(id)
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fight_id INTEGER NOT NULL,
    fighter1_win_prob REAL,
    fighter2_win_prob REAL,
    predicted_winner_id INTEGER,
    edge_fighter1 REAL,
    edge_fighter2 REAL,
    kelly_fighter1 REAL,
    kelly_fighter2 REAL,
    confidence TEXT,
    method_ko_prob REAL,
    method_sub_prob REAL,
    method_dec_prob REAL,
    over_rounds_prob REAL,
    under_rounds_prob REAL,
    model_version TEXT,
    created_at TEXT,
    FOREIGN KEY (fight_id) REFERENCES fights(id)
);

CREATE TABLE IF NOT EXISTS results_tracker (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL,
    fight_id INTEGER NOT NULL,
    predicted_winner_id INTEGER,
    actual_winner_id INTEGER,
    was_correct INTEGER,
    predicted_prob REAL,
    edge_at_prediction REAL,
    kelly_size REAL,
    profit_loss REAL,
    created_at TEXT,
    FOREIGN KEY (prediction_id) REFERENCES predictions(id),
    FOREIGN KEY (fight_id) REFERENCES fights(id)
);

CREATE INDEX IF NOT EXISTS idx_fighters_name ON fighters(name);
CREATE INDEX IF NOT EXISTS idx_fights_event ON fights(event_id);
CREATE INDEX IF NOT EXISTS idx_fight_stats_fight ON fight_stats(fight_id);
CREATE INDEX IF NOT EXISTS idx_elo_fighter ON elo_ratings(fighter_id);
CREATE INDEX IF NOT EXISTS idx_odds_fight ON odds(fight_id);
CREATE INDEX IF NOT EXISTS idx_predictions_fight ON predictions(fight_id);
"""


# --- Helper functions ---

def upsert_fighter(conn, name, **kwargs):
    """Insert or update a fighter, return fighter id."""
    kwargs["updated_at"] = datetime.utcnow().isoformat()
    kwargs["name"] = name

    existing = conn.execute(
        "SELECT id FROM fighters WHERE name = ? OR ufcstats_url = ?",
        (name, kwargs.get("ufcstats_url", ""))
    ).fetchone()

    if existing:
        sets = ", ".join(f"{k} = ?" for k in kwargs if k != "name")
        vals = [v for k, v in kwargs.items() if k != "name"]
        vals.append(existing["id"])
        if sets:
            conn.execute(f"UPDATE fighters SET {sets} WHERE id = ?", vals)
        return existing["id"]

    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join("?" for _ in kwargs)
    cur = conn.execute(
        f"INSERT INTO fighters ({cols}) VALUES ({placeholders})",
        list(kwargs.values())
    )
    return cur.lastrowid


def upsert_event(conn, name, **kwargs):
    """Insert or update an event, return event id."""
    kwargs["updated_at"] = datetime.utcnow().isoformat()
    kwargs["name"] = name

    existing = conn.execute(
        "SELECT id FROM events WHERE name = ? OR ufcstats_url = ?",
        (name, kwargs.get("ufcstats_url", ""))
    ).fetchone()

    if existing:
        sets = ", ".join(f"{k} = ?" for k in kwargs if k != "name")
        vals = [v for k, v in kwargs.items() if k != "name"]
        vals.append(existing["id"])
        if sets:
            conn.execute(f"UPDATE events SET {sets} WHERE id = ?", vals)
        return existing["id"]

    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join("?" for _ in kwargs)
    cur = conn.execute(
        f"INSERT INTO events ({cols}) VALUES ({placeholders})",
        list(kwargs.values())
    )
    return cur.lastrowid


def upsert_fight(conn, event_id, fighter1_id, fighter2_id, **kwargs):
    """Insert or update a fight, return fight id."""
    kwargs["updated_at"] = datetime.utcnow().isoformat()
    kwargs["event_id"] = event_id
    kwargs["fighter1_id"] = fighter1_id
    kwargs["fighter2_id"] = fighter2_id

    existing = None
    if kwargs.get("ufcstats_url"):
        existing = conn.execute(
            "SELECT id FROM fights WHERE ufcstats_url = ?",
            (kwargs["ufcstats_url"],)
        ).fetchone()

    if not existing:
        existing = conn.execute(
            "SELECT id FROM fights WHERE event_id = ? AND "
            "((fighter1_id = ? AND fighter2_id = ?) OR "
            " (fighter1_id = ? AND fighter2_id = ?))",
            (event_id, fighter1_id, fighter2_id, fighter2_id, fighter1_id)
        ).fetchone()

    if existing:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [existing["id"]]
        conn.execute(f"UPDATE fights SET {sets} WHERE id = ?", vals)
        return existing["id"]

    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join("?" for _ in kwargs)
    cur = conn.execute(
        f"INSERT INTO fights ({cols}) VALUES ({placeholders})",
        list(kwargs.values())
    )
    return cur.lastrowid


def get_fighter_fights(conn, fighter_id, limit=None):
    """Get all fights for a fighter, most recent first."""
    query = """
        SELECT f.*, e.date as event_date, e.name as event_name
        FROM fights f
        JOIN events e ON f.event_id = e.id
        WHERE (f.fighter1_id = ? OR f.fighter2_id = ?) AND e.date IS NOT NULL
        ORDER BY e.date DESC
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    return conn.execute(query, (fighter_id, fighter_id)).fetchall()


def get_fighter_by_name(conn, name):
    """Fuzzy match fighter by name."""
    row = conn.execute("SELECT * FROM fighters WHERE name = ?", (name,)).fetchone()
    if row:
        return row
    # try case-insensitive
    row = conn.execute(
        "SELECT * FROM fighters WHERE LOWER(name) = LOWER(?)", (name,)
    ).fetchone()
    if row:
        return row
    # try partial
    row = conn.execute(
        "SELECT * FROM fighters WHERE LOWER(name) LIKE LOWER(?)",
        (f"%{name}%",)
    ).fetchone()
    return row


def get_upcoming_events(conn):
    """Get events that haven't happened yet."""
    return conn.execute(
        "SELECT * FROM events WHERE is_completed = 0 ORDER BY date ASC"
    ).fetchall()


def get_completed_events(conn, limit=50):
    """Get completed events, most recent first."""
    return conn.execute(
        "SELECT * FROM events WHERE is_completed = 1 ORDER BY date DESC LIMIT ?",
        (limit,)
    ).fetchall()


def get_event_fights(conn, event_id):
    """Get all fights for an event."""
    return conn.execute(
        """SELECT f.*,
           f1.name as fighter1_name, f2.name as fighter2_name,
           f1.reach_inches as fighter1_reach, f2.reach_inches as fighter2_reach,
           f1.height_inches as fighter1_height, f2.height_inches as fighter2_height,
           f1.stance as fighter1_stance, f2.stance as fighter2_stance
        FROM fights f
        JOIN fighters f1 ON f.fighter1_id = f1.id
        JOIN fighters f2 ON f.fighter2_id = f2.id
        WHERE f.event_id = ?
        ORDER BY f.is_main_event DESC, f.id ASC""",
        (event_id,)
    ).fetchall()
