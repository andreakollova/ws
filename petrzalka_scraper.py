#!/usr/bin/env python3
"""
FC Petržalka ženy — upcoming match scraper via FlashScore.
Parses embedded fixture data from the team page (no API key needed).
"""

import logging
import re
import time
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

TEAM_URL = "https://www.flashscore.sk/tim/fc-petrzalka/K0sAKyf1/"
TEAM_NAME = "Petržalka Ž"
PHOTO_URL = "https://mediamanager.ws/images/27x11x1182x887-1920x1440xmedia/pages/f/futbalsfz.sk/2025/11/petrzalka-zeny-n.jpg.webp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WoevaEventsBot/1.0; +https://woeva.com)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.5",
}
# Slovakia is UTC+2 in summer
CET = timezone(timedelta(hours=2))


def scrape_petrzalka(existing_urls: set) -> list[dict]:
    """Return upcoming FC Petržalka ženy matches as event dicts."""
    logger.info(f"Fetching FlashScore fixtures: {TEAM_URL}")
    try:
        r = requests.get(TEAM_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"FlashScore fetch failed: {e}")
        return []

    matches = _parse_fixtures(r.text)
    logger.info(f"  Found {len(matches)} upcoming matches")

    events = []
    for match in matches:
        url = match["url"]
        if url in existing_urls:
            logger.debug(f"Skip (known): {url}")
            continue
        event = _match_to_event(match)
        events.append(event)
        existing_urls.add(url)
        logger.info(f"Match event: {event['title']}")

    return events


def _parse_fixtures(html: str) -> list[dict]:
    """Extract upcoming Petržalka matches from embedded FlashScore data."""
    m = re.search(
        r'cjs\.initialFeeds\["fixtures"\]\s*=\s*\{\s*data:\s*`(.*?)`',
        html, re.S
    )
    if not m:
        logger.warning("FlashScore fixtures data not found in page")
        return []

    now = datetime.now(tz=CET)
    matches = []

    for block in m.group(1).split("~"):
        fields = {}
        for part in block.split("¬"):
            if "÷" in part:
                k, v = part.split("÷", 1)
                fields[k] = v

        if "AD" not in fields or "CX" not in fields or "AF" not in fields:
            continue

        # Skip if neither team is Petržalka
        home = fields["CX"]
        away = fields["AF"]
        if TEAM_NAME not in home and TEAM_NAME not in away:
            continue

        try:
            ts = int(fields["AD"])
            dt = datetime.fromtimestamp(ts, tz=CET)
        except (ValueError, OSError):
            continue

        # Skip past matches
        if dt < now:
            continue

        match_id = fields.get("AA", "")
        home_slug = fields.get("WU", "")
        away_slug = fields.get("WV", "")
        url = f"https://www.flashscore.sk/zapas/{home_slug}-{away_slug}/{match_id}/"

        matches.append({
            "home": home,
            "away": away,
            "date": dt.strftime("%Y-%m-%d"),
            "time": dt.strftime("%H:%M"),
            "url": url,
            "is_home": TEAM_NAME in home,
        })

    return matches


def _match_to_event(match: dict) -> dict:
    home = match["home"]
    away = match["away"]
    is_home = match["is_home"]
    opponent = away if is_home else home

    title = f"{home} – {away}"

    location_note = "Petržalka, Bratislava" if is_home else f"Zápas mimo domova vs {opponent}"
    original_description = (
        f"Zápas 1. ligy žien, Skupina o titul. {title}. "
        f"{'Domáci zápas' if is_home else 'Hosťujúci zápas'} FC Petržalka ženy."
    )

    return {
        "source_url": match["url"],
        "source": "petrzalka",
        "title": title,
        "original_description": original_description,
        "date": match["date"],
        "time_start": match["time"],
        "duration": "90min",
        "venue": "Štadión FC Petržalka" if is_home else "",
        "address": "Petržalka, Bratislava" if is_home else "",
        "city": "Bratislava",
        "photo_url": PHOTO_URL,
        "min_price": 0.0,
        "max_price": 0.0,
        "page_free": True,
    }
