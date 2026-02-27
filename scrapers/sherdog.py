"""Scraper for Sherdog.com — supplemental fighter data."""

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
    try:
        resp = requests.get(url, headers=headers, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Sherdog fetch failed for %s: %s", url, e)
        return None
    set_cached(url, resp.text)
    return resp.text


def search_fighter(name):
    """Search Sherdog for a fighter by name, return profile URL or None."""
    search_url = f"{config.SHERDOG_BASE}/stats/fightfinder?SearchTxt={name.replace(' ', '+')}"
    html = _get(search_url)
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    # Look for fighter result rows
    rows = soup.select("tr")
    for row in rows:
        link = row.select_one("a[href*='/fighter/']")
        if link:
            href = link.get("href", "")
            link_name = link.get_text(strip=True).lower()
            if name.lower() in link_name or link_name in name.lower():
                if href.startswith("/"):
                    return config.SHERDOG_BASE + href
                return href
    return None


def scrape_fighter_profile(url):
    """Scrape a Sherdog fighter profile for supplemental data."""
    if not url:
        return {}
    html = _get(url)
    if not html:
        return {}
    soup = BeautifulSoup(html, "lxml")

    info = {"sherdog_url": url}

    # Age / DOB
    bday = soup.select_one("span[itemprop='birthDate']")
    if bday:
        try:
            info["dob"] = datetime.strptime(
                bday.get_text(strip=True), "%Y-%m-%d"
            ).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Nationality
    nation = soup.select_one("strong[itemprop='nationality']")
    if nation:
        info["nationality"] = nation.get_text(strip=True)

    # Association / team
    assoc = soup.select_one("span[itemprop='memberOf'] span[itemprop='name']")
    if assoc:
        info["team"] = assoc.get_text(strip=True)

    # Height
    height_el = soup.select_one("span[itemprop='height']")
    if height_el:
        h = height_el.get_text(strip=True)
        m = re.search(r"(\d+)'[\s]*(\d+)", h)
        if m:
            info["height_inches"] = int(m.group(1)) * 12 + int(m.group(2))

    # Weight class
    wc = soup.select_one("h6.item.wclass a")
    if wc:
        info["weight_class"] = wc.get_text(strip=True)

    # Win/Loss record (as backup)
    wins_el = soup.select_one("span.counter")
    # Fight history — count methods
    fight_rows = soup.select("div.module.fight_history tr")
    methods = {"ko": 0, "sub": 0, "dec": 0, "total_wins": 0}
    for row in fight_rows:
        cols = row.select("td")
        if len(cols) < 4:
            continue
        result = cols[0].get_text(strip=True).lower()
        method = cols[3].get_text(strip=True).lower() if len(cols) > 3 else ""
        if result == "win":
            methods["total_wins"] += 1
            if "ko" in method or "tko" in method:
                methods["ko"] += 1
            elif "sub" in method:
                methods["sub"] += 1
            elif "dec" in method:
                methods["dec"] += 1

    if methods["total_wins"] > 0:
        info["ko_rate"] = methods["ko"] / methods["total_wins"]
        info["sub_rate"] = methods["sub"] / methods["total_wins"]
        info["dec_rate"] = methods["dec"] / methods["total_wins"]
        info["finish_rate"] = (methods["ko"] + methods["sub"]) / methods["total_wins"]

    return info
