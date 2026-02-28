"""Scraper for UFCStats.com — events, fights, and fighter details."""

import re
import time
import logging
from datetime import datetime

import requests
from bs4 import BeautifulSoup

import config
from scrapers.cache import get_cached, set_cached

logger = logging.getLogger(__name__)


def _get(url):
    """Fetch a URL with caching and rate limiting."""
    cached = get_cached(url)
    if cached:
        return cached
    time.sleep(config.RATE_LIMIT_DELAY)
    headers = {"User-Agent": config.USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=config.REQUEST_TIMEOUT)
    resp.raise_for_status()
    set_cached(url, resp.text)
    return resp.text


def _parse_height(text):
    """Parse '5\\'10\"' or '5\\' 10\"' to inches."""
    if not text or text.strip() == "--":
        return None
    m = re.search(r"(\d+)'[\s]*(\d+)\"?", text.strip())
    if m:
        return int(m.group(1)) * 12 + int(m.group(2))
    return None


def _parse_reach(text):
    """Parse '72\"' or '72' to float."""
    if not text or text.strip() == "--":
        return None
    m = re.search(r"([\d.]+)", text.strip())
    if m:
        return float(m.group(1))
    return None


def _parse_pct(text):
    """Parse '52%' to 0.52."""
    if not text or text.strip() == "--":
        return None
    m = re.search(r"([\d.]+)", text.strip())
    if m:
        return float(m.group(1)) / 100.0
    return None


def _parse_float(text):
    """Parse a float from text."""
    if not text or text.strip() == "--":
        return None
    m = re.search(r"([\d.]+)", text.strip())
    if m:
        return float(m.group(1))
    return None


def _parse_ctrl_time(text):
    """Parse '5:23' to seconds."""
    if not text or text.strip() == "--":
        return None
    m = re.search(r"(\d+):(\d+)", text.strip())
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


def _parse_date(text):
    """Parse various date formats from UFCStats into YYYY-MM-DD."""
    if not text or text.strip() in ("--", ""):
        return None
    text = text.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%b. %d, %Y",
                "%Y-%m-%d", "%m/%d/%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Last resort: try to find a date pattern
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return m.group(0)
    logger.warning("Could not parse date: %r", text)
    return None


# ---------------------------------------------------------------------------
# Event list scraping
# ---------------------------------------------------------------------------

def scrape_completed_events(max_pages=3):
    """Scrape list of completed events from UFCStats."""
    events = []
    for page in range(1, max_pages + 1):
        url = f"{config.UFCSTATS_BASE}?page={page}"
        logger.info("Scraping completed events page %d: %s", page, url)
        html = _get(url)
        soup = BeautifulSoup(html, "lxml")
        rows = soup.select("tr.b-statistics__table-row")
        for row in rows:
            link = row.select_one("a.b-link")
            if not link:
                continue
            name = link.get_text(strip=True)
            href = link.get("href", "").strip()
            date_cell = row.select("td")
            date_str = None
            location = None
            if len(date_cell) >= 2:
                date_str = date_cell[1].get_text(strip=True) if date_cell[1] else None
            if len(date_cell) >= 3:
                location = date_cell[2].get_text(strip=True) if date_cell[2] else None
            parsed_date = _parse_date(date_str)
            if name and href:
                events.append({
                    "name": name,
                    "date": parsed_date,
                    "location": location,
                    "ufcstats_url": href,
                    "is_completed": 1,
                })
    logger.info("Found %d completed events", len(events))
    return events


def scrape_upcoming_events():
    """Scrape upcoming events from UFCStats."""
    events = []
    url = config.UFCSTATS_UPCOMING
    logger.info("Scraping upcoming events: %s", url)
    try:
        html = _get(url)
    except requests.RequestException as e:
        logger.warning("Failed to fetch upcoming events: %s", e)
        return events
    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("tr.b-statistics__table-row")
    for row in rows:
        link = row.select_one("a.b-link")
        if not link:
            continue
        name = link.get_text(strip=True)
        href = link.get("href", "").strip()
        date_cell = row.select("td")
        date_str = None
        location = None
        if len(date_cell) >= 2:
            date_str = date_cell[1].get_text(strip=True) if date_cell[1] else None
        if len(date_cell) >= 3:
            location = date_cell[2].get_text(strip=True) if date_cell[2] else None
        parsed_date = _parse_date(date_str)
        if name and href:
            events.append({
                "name": name,
                "date": parsed_date,
                "location": location,
                "ufcstats_url": href,
                "is_completed": 0,
            })
    logger.info("Found %d upcoming events", len(events))
    return events


# ---------------------------------------------------------------------------
# Event detail / fight card scraping
# ---------------------------------------------------------------------------

def scrape_event_fights(event_url):
    """Scrape the fight card for a single event, returns list of fight dicts."""
    logger.info("Scraping event fights: %s", event_url)
    html = _get(event_url)
    soup = BeautifulSoup(html, "lxml")
    fights = []

    rows = soup.select("tr.b-fight-details__table-row")
    for i, row in enumerate(rows):
        # skip header row
        if row.select_one("th"):
            continue
        cols = row.select("td")
        if len(cols) < 8:
            continue

        fight_link = row.get("data-link", "").strip()
        fighters = cols[1].select("a")
        if len(fighters) < 2:
            continue

        f1_name = fighters[0].get_text(strip=True)
        f2_name = fighters[1].get_text(strip=True)
        f1_url = fighters[0].get("href", "").strip()
        f2_url = fighters[1].get("href", "").strip()

        # winner detection: first column has W/L/D/NC
        results = cols[0].select("p")
        winner = None
        if len(results) >= 2:
            r1 = results[0].get_text(strip=True).upper()
            r2 = results[1].get_text(strip=True).upper()
            if r1 == "W":
                winner = "fighter1"
            elif r1 == "L":
                winner = "fighter2"
            elif r2 == "W":
                winner = "fighter2"
            if not winner:
                logger.debug("No winner detected for %s vs %s (r1=%r, r2=%r)",
                             f1_name, f2_name, r1, r2)
        else:
            # Try i-tag based detection (newer UFCStats layout)
            win_tags = cols[0].select("i.b-flag__text")
            if not win_tags:
                win_tags = cols[0].select("i")
            for wt in win_tags:
                txt = wt.get_text(strip=True).upper()
                if txt == "WIN":
                    winner = "fighter1"
                    break
            if not winner and len(results) == 0:
                logger.debug("No W/L markers found for %s vs %s (col0 html: %s)",
                             f1_name, f2_name, str(cols[0])[:200])

        # weight class
        wc_el = cols[6] if len(cols) > 6 else None
        weight_class = wc_el.get_text(strip=True) if wc_el else None

        # method
        method_el = cols[7] if len(cols) > 7 else None
        method = method_el.get_text(strip=True) if method_el else None

        # round
        round_el = cols[8] if len(cols) > 8 else None
        rnd = None
        if round_el:
            try:
                rnd = int(round_el.get_text(strip=True))
            except ValueError:
                pass

        # time
        time_el = cols[9] if len(cols) > 9 else None
        fight_time = time_el.get_text(strip=True) if time_el else None

        fights.append({
            "fighter1_name": f1_name,
            "fighter2_name": f2_name,
            "fighter1_url": f1_url,
            "fighter2_url": f2_url,
            "winner": winner,
            "weight_class": weight_class,
            "method": method,
            "round": rnd,
            "time": fight_time,
            "ufcstats_url": fight_link,
            "is_main_event": 1 if i == 1 else 0,  # first data row after header
        })

    logger.info("Found %d fights for event", len(fights))
    return fights


# ---------------------------------------------------------------------------
# Fighter detail scraping
# ---------------------------------------------------------------------------

def scrape_fighter_details(fighter_url):
    """Scrape detailed stats for a single fighter."""
    logger.info("Scraping fighter details: %s", fighter_url)
    html = _get(fighter_url)
    soup = BeautifulSoup(html, "lxml")

    info = {"ufcstats_url": fighter_url}

    # Name
    name_el = soup.select_one("span.b-content__title-highlight")
    if name_el:
        info["name"] = name_el.get_text(strip=True)

    # Nickname
    nick_el = soup.select_one("p.b-content__Nickname")
    if nick_el:
        info["nickname"] = nick_el.get_text(strip=True).strip('"')

    # Record
    record_el = soup.select_one("span.b-content__title-record")
    if record_el:
        record_text = record_el.get_text(strip=True)
        m = re.search(r"(\d+)-(\d+)-(\d+)", record_text)
        if m:
            info["record_wins"] = int(m.group(1))
            info["record_losses"] = int(m.group(2))
            info["record_draws"] = int(m.group(3))
        nc = re.search(r"(\d+)\s*NC", record_text)
        if nc:
            info["record_nc"] = int(nc.group(1))

    # Bio items (height, weight, reach, stance, DOB)
    bio_items = soup.select("ul.b-list__box-list li")
    for item in bio_items:
        text = item.get_text(strip=True)
        if "Height:" in text:
            info["height_inches"] = _parse_height(text.split("Height:")[-1])
        elif "Reach:" in text:
            info["reach_inches"] = _parse_reach(text.split("Reach:")[-1])
        elif "STANCE:" in text or "Stance:" in text:
            stance = text.split(":")[-1].strip()
            if stance and stance != "--":
                info["stance"] = stance
        elif "DOB:" in text:
            dob = text.split("DOB:")[-1].strip()
            if dob and dob != "--":
                for fmt in ("%b %d, %Y", "%B %d, %Y"):
                    try:
                        info["dob"] = datetime.strptime(dob, fmt).strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        continue

    # Career stats box
    stats = {}
    stat_boxes = soup.select("div.b-list__info-box-left li, div.b-list__info-box li")
    for box in stat_boxes:
        text = box.get_text(" ", strip=True)
        if "SLpM:" in text:
            stats["slpm"] = _parse_float(text.split("SLpM:")[-1])
        elif "Str. Acc.:" in text:
            stats["str_acc"] = _parse_pct(text.split("Str. Acc.:")[-1])
        elif "SApM:" in text:
            stats["sapm"] = _parse_float(text.split("SApM:")[-1])
        elif "Str. Def:" in text or "Str. Def.:" in text:
            stats["str_def"] = _parse_pct(text.split(":")[-1])
        elif "TD Avg.:" in text:
            stats["td_avg"] = _parse_float(text.split("TD Avg.:")[-1])
        elif "TD Acc.:" in text:
            stats["td_acc"] = _parse_pct(text.split("TD Acc.:")[-1])
        elif "TD Def.:" in text:
            stats["td_def"] = _parse_pct(text.split("TD Def.:")[-1])
        elif "Sub. Avg.:" in text:
            stats["sub_avg"] = _parse_float(text.split("Sub. Avg.:")[-1])

    info["stats"] = stats
    return info


# ---------------------------------------------------------------------------
# Fight detail scraping (per-round stats)
# ---------------------------------------------------------------------------

def scrape_fight_details(fight_url):
    """Scrape detailed per-fighter totals for a single fight."""
    if not fight_url:
        return []
    logger.info("Scraping fight details: %s", fight_url)
    html = _get(fight_url)
    soup = BeautifulSoup(html, "lxml")

    results = []

    # Totals section
    totals_section = soup.select("section.b-fight-details__section")
    for section in totals_section:
        tables = section.select("table")
        for table in tables:
            rows = table.select("tr")
            for row in rows:
                cols = row.select("td")
                if len(cols) < 9:
                    continue
                fighters_in_col = cols[0].select("p")
                if len(fighters_in_col) < 2:
                    continue

                for idx in range(2):
                    name = fighters_in_col[idx].get_text(strip=True)
                    stat = {"name": name}

                    # Parse columns — Totals table
                    def _get_col_val(col_idx, fighter_idx):
                        ps = cols[col_idx].select("p")
                        if len(ps) > fighter_idx:
                            return ps[fighter_idx].get_text(strip=True)
                        return ""

                    # KD
                    kd = _get_col_val(1, idx)
                    stat["knockdowns"] = int(kd) if kd.isdigit() else 0

                    # Sig. str
                    sig = _get_col_val(2, idx)
                    m = re.match(r"(\d+)\s*of\s*(\d+)", sig)
                    if m:
                        stat["sig_str_landed"] = int(m.group(1))
                        stat["sig_str_attempted"] = int(m.group(2))

                    # Total str
                    tstr = _get_col_val(4, idx)
                    m = re.match(r"(\d+)\s*of\s*(\d+)", tstr)
                    if m:
                        stat["total_str_landed"] = int(m.group(1))
                        stat["total_str_attempted"] = int(m.group(2))

                    # TD
                    td = _get_col_val(5, idx)
                    m = re.match(r"(\d+)\s*of\s*(\d+)", td)
                    if m:
                        stat["td_landed"] = int(m.group(1))
                        stat["td_attempted"] = int(m.group(2))

                    # Sub att
                    sub = _get_col_val(6, idx)
                    stat["sub_att"] = int(sub) if sub.isdigit() else 0

                    # Rev
                    rev = _get_col_val(7, idx)
                    stat["rev"] = int(rev) if rev.isdigit() else 0

                    # Ctrl
                    ctrl = _get_col_val(8, idx)
                    stat["ctrl_time_seconds"] = _parse_ctrl_time(ctrl) or 0

                    results.append(stat)
                # only parse first data row in totals
                break
            break
        break

    return results
