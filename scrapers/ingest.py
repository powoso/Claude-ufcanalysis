"""Data ingestion pipeline — orchestrates scraping and stores data in the DB."""

import logging

from scrapers.ufcstats import (
    scrape_completed_events,
    scrape_upcoming_events,
    scrape_event_fights,
    scrape_fighter_details,
    scrape_fight_details,
)
from scrapers.sherdog import search_fighter, scrape_fighter_profile
from scrapers.odds_api import fetch_odds, store_odds_snapshot
from database import (
    upsert_fighter, upsert_event, upsert_fight, get_db,
)

logger = logging.getLogger(__name__)


def ingest_events(conn, max_pages=3, include_upcoming=True):
    """Scrape and store all events."""
    events_data = scrape_completed_events(max_pages=max_pages)
    if include_upcoming:
        events_data.extend(scrape_upcoming_events())

    event_ids = []
    for ev in events_data:
        eid = upsert_event(
            conn, ev["name"],
            date=ev.get("date"),
            location=ev.get("location"),
            is_completed=ev.get("is_completed", 0),
            ufcstats_url=ev.get("ufcstats_url"),
        )
        event_ids.append((eid, ev))

    conn.commit()
    logger.info("Ingested %d events", len(event_ids))
    return event_ids


def ingest_event_fights(conn, event_id, event_url):
    """Scrape and store all fights for one event."""
    fights_data = scrape_event_fights(event_url)
    fight_ids = []

    winners_found = 0
    for fd in fights_data:
        # Ensure fighters exist
        f1_id = upsert_fighter(
            conn, fd["fighter1_name"],
            ufcstats_url=fd.get("fighter1_url"),
        )
        f2_id = upsert_fighter(
            conn, fd["fighter2_name"],
            ufcstats_url=fd.get("fighter2_url"),
        )

        # Determine winner id
        winner_id = None
        if fd.get("winner") == "fighter1":
            winner_id = f1_id
            winners_found += 1
        elif fd.get("winner") == "fighter2":
            winner_id = f2_id
            winners_found += 1

        fid = upsert_fight(
            conn, event_id, f1_id, f2_id,
            winner_id=winner_id,
            weight_class=fd.get("weight_class"),
            method=fd.get("method"),
            round=fd.get("round"),
            time=fd.get("time"),
            is_main_event=fd.get("is_main_event", 0),
            ufcstats_url=fd.get("ufcstats_url"),
        )
        fight_ids.append((fid, fd))

    conn.commit()
    logger.info("Ingested %d fights for event %d (%d with winners)",
                len(fight_ids), event_id, winners_found)
    return fight_ids


def ingest_fighter_details(conn, fighter_id, fighter_url):
    """Scrape and store detailed stats for a fighter."""
    if not fighter_url:
        return

    details = scrape_fighter_details(fighter_url)
    if not details:
        return

    # Update fighter record
    update_kwargs = {}
    for key in ["nickname", "height_inches", "reach_inches", "stance", "dob",
                 "record_wins", "record_losses", "record_draws", "record_nc"]:
        if key in details and details[key] is not None:
            update_kwargs[key] = details[key]

    if update_kwargs:
        sets = ", ".join(f"{k} = ?" for k in update_kwargs)
        vals = list(update_kwargs.values()) + [fighter_id]
        conn.execute(f"UPDATE fighters SET {sets} WHERE id = ?", vals)

    # Store career stats
    stats = details.get("stats", {})
    if stats:
        existing = conn.execute(
            "SELECT id FROM fighter_stats WHERE fighter_id = ?", (fighter_id,)
        ).fetchone()
        if existing:
            sets = ", ".join(f"{k} = ?" for k in stats)
            vals = list(stats.values()) + [existing["id"]]
            conn.execute(f"UPDATE fighter_stats SET {sets} WHERE id = ?", vals)
        else:
            stats["fighter_id"] = fighter_id
            cols = ", ".join(stats.keys())
            placeholders = ", ".join("?" for _ in stats)
            conn.execute(
                f"INSERT INTO fighter_stats ({cols}) VALUES ({placeholders})",
                list(stats.values())
            )

    conn.commit()


def ingest_fight_details(conn, fight_id, fight_url):
    """Scrape and store per-fighter stats for a single fight."""
    if not fight_url:
        return

    stats_list = scrape_fight_details(fight_url)
    for stat in stats_list:
        fighter_name = stat.pop("name", "")
        if not fighter_name:
            continue

        # Find fighter ID
        fighter = conn.execute(
            "SELECT id FROM fighters WHERE LOWER(name) = LOWER(?)",
            (fighter_name,)
        ).fetchone()
        if not fighter:
            # Try partial match
            fighter = conn.execute(
                "SELECT id FROM fighters WHERE LOWER(name) LIKE LOWER(?)",
                (f"%{fighter_name}%",)
            ).fetchone()
        if not fighter:
            continue

        stat["fight_id"] = fight_id
        stat["fighter_id"] = fighter["id"]

        existing = conn.execute(
            "SELECT id FROM fight_stats WHERE fight_id = ? AND fighter_id = ?",
            (fight_id, fighter["id"])
        ).fetchone()

        if existing:
            sets = ", ".join(f"{k} = ?" for k in stat if k not in ("fight_id", "fighter_id"))
            vals = [v for k, v in stat.items() if k not in ("fight_id", "fighter_id")]
            vals.append(existing["id"])
            if sets:
                conn.execute(f"UPDATE fight_stats SET {sets} WHERE id = ?", vals)
        else:
            cols = ", ".join(stat.keys())
            placeholders = ", ".join("?" for _ in stat)
            conn.execute(
                f"INSERT INTO fight_stats ({cols}) VALUES ({placeholders})",
                list(stat.values())
            )

    conn.commit()


def enrich_fighters_sherdog(conn, limit=50):
    """Enrich fighter data using Sherdog (team, DOB, method rates)."""
    fighters = conn.execute(
        "SELECT id, name, sherdog_url FROM fighters WHERE sherdog_url IS NULL LIMIT ?",
        (limit,)
    ).fetchall()

    enriched = 0
    for f in fighters:
        url = search_fighter(f["name"])
        if not url:
            continue
        profile = scrape_fighter_profile(url)
        if not profile:
            continue

        update_kwargs = {}
        for key in ["dob", "team", "height_inches", "weight_class", "sherdog_url"]:
            if key in profile and profile[key]:
                update_kwargs[key] = profile[key]

        if update_kwargs:
            sets = ", ".join(f"{k} = ?" for k in update_kwargs)
            vals = list(update_kwargs.values()) + [f["id"]]
            conn.execute(f"UPDATE fighters SET {sets} WHERE id = ?", vals)

        # Store method rates if available
        rate_keys = ["ko_rate", "sub_rate", "dec_rate", "finish_rate"]
        rates = {k: profile[k] for k in rate_keys if k in profile}
        if rates:
            existing = conn.execute(
                "SELECT id FROM fighter_stats WHERE fighter_id = ?", (f["id"],)
            ).fetchone()
            if existing:
                sets = ", ".join(f"{k} = ?" for k in rates)
                vals = list(rates.values()) + [existing["id"]]
                conn.execute(f"UPDATE fighter_stats SET {sets} WHERE id = ?", vals)
            else:
                rates["fighter_id"] = f["id"]
                cols = ", ".join(rates.keys())
                placeholders = ", ".join("?" for _ in rates)
                conn.execute(
                    f"INSERT INTO fighter_stats ({cols}) VALUES ({placeholders})",
                    list(rates.values())
                )

        enriched += 1

    conn.commit()
    logger.info("Enriched %d fighters from Sherdog", enriched)
    return enriched


def ingest_odds(conn):
    """Fetch and store current odds."""
    odds_data = fetch_odds()
    if odds_data:
        store_odds_snapshot(conn, odds_data)
        conn.commit()
    logger.info("Ingested odds for %d fights", len(odds_data))
    return odds_data


def full_ingest(conn, max_event_pages=3, max_fight_details=100,
                max_fighter_details=200, sherdog_limit=50):
    """Run the full data ingestion pipeline.

    1. Scrape events (completed + upcoming)
    2. Scrape fight cards for each event
    3. Scrape detailed fighter stats
    4. Scrape per-fight stats
    5. Enrich from Sherdog
    6. Fetch odds
    """
    logger.info("=== Starting full data ingestion ===")

    # Step 1: Events
    logger.info("Step 1: Ingesting events...")
    event_pairs = ingest_events(conn, max_pages=max_event_pages)

    # Step 2: Fight cards
    logger.info("Step 2: Ingesting fight cards...")
    all_fights = []
    for event_id, event_data in event_pairs:
        url = event_data.get("ufcstats_url")
        if url:
            fight_pairs = ingest_event_fights(conn, event_id, url)
            all_fights.extend(fight_pairs)

    # Step 3: Fighter details (for fighters we haven't detailed yet)
    logger.info("Step 3: Ingesting fighter details...")
    fighters = conn.execute(
        """SELECT id, ufcstats_url FROM fighters
           WHERE ufcstats_url IS NOT NULL
           ORDER BY updated_at ASC LIMIT ?""",
        (max_fighter_details,)
    ).fetchall()
    for f in fighters:
        try:
            ingest_fighter_details(conn, f["id"], f["ufcstats_url"])
        except Exception as e:
            logger.warning("Failed to ingest fighter %d: %s", f["id"], e)

    # Step 4: Fight details
    logger.info("Step 4: Ingesting fight details...")
    fights = conn.execute(
        """SELECT f.id, f.ufcstats_url FROM fights f
           LEFT JOIN fight_stats fs ON f.id = fs.fight_id
           WHERE f.ufcstats_url IS NOT NULL AND fs.id IS NULL
           LIMIT ?""",
        (max_fight_details,)
    ).fetchall()
    for f in fights:
        try:
            ingest_fight_details(conn, f["id"], f["ufcstats_url"])
        except Exception as e:
            logger.warning("Failed to ingest fight details %d: %s", f["id"], e)

    # Step 5: Sherdog enrichment
    logger.info("Step 5: Enriching from Sherdog...")
    try:
        enrich_fighters_sherdog(conn, limit=sherdog_limit)
    except Exception as e:
        logger.warning("Sherdog enrichment failed: %s", e)

    # Step 6: Odds
    logger.info("Step 6: Fetching odds...")
    odds_data = ingest_odds(conn)

    # Diagnostics
    total_fighters = conn.execute("SELECT COUNT(*) as cnt FROM fighters").fetchone()["cnt"]
    total_fights = conn.execute("SELECT COUNT(*) as cnt FROM fights").fetchone()["cnt"]
    fights_with_winner = conn.execute(
        "SELECT COUNT(*) as cnt FROM fights WHERE winner_id IS NOT NULL"
    ).fetchone()["cnt"]
    events_with_date = conn.execute(
        "SELECT COUNT(*) as cnt FROM events WHERE date IS NOT NULL"
    ).fetchone()["cnt"]
    fight_stats_count = conn.execute("SELECT COUNT(*) as cnt FROM fight_stats").fetchone()["cnt"]
    fighter_stats_count = conn.execute("SELECT COUNT(*) as cnt FROM fighter_stats").fetchone()["cnt"]

    logger.info("=== Full ingestion complete ===")
    logger.info("DB summary: %d fighters, %d fights (%d with winners), "
                "%d events with dates, %d fight_stats rows, %d fighter_stats rows",
                total_fighters, total_fights, fights_with_winner,
                events_with_date, fight_stats_count, fighter_stats_count)
    return odds_data
