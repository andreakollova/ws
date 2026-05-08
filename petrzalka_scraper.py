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
GITHUB_RAW = "https://raw.githubusercontent.com/andreakollova/ws/main/photos/petrzalka"

# Opponent keyword → photo filename (lowercase match)
OPPONENT_PHOTOS = {
    "bardejov": "bardejov.jpg",
    "prešov":   "presov.jpg",
    "presov":   "presov.jpg",
    "žilina":   "zilina.jpg",
    "zilina":   "zilina.jpg",
}
# Generic photos cycled for opponents without a specific photo
GENERIC_PHOTOS = ["1.jpg", "4.jpg", "5.jpg", "6.jpg", "7.jpg"]

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
    for index, match in enumerate(matches):
        match["index"] = index
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

        # Skip if Petržalka is not playing at home
        home = fields["CX"]
        away = fields["AF"]
        if TEAM_NAME not in home:
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
    opponent = away  # always home matches

    title = f"{home} – {away}"

    original_description = (
        f"Domáci zápas FC Petržalka ženy v 1. lige žien (Skupina o titul). "
        f"Súper: {opponent}. Príďte povzbudiť naše futbalistky na štadióne v Petržalke!"
    )

    photo_url = _pick_photo(opponent, match.get("index", 0))

    return {
        "source_url": match["url"],
        "source": "petrzalka",
        "title": title,
        "original_description": original_description,
        "date": match["date"],
        "time_start": match["time"],
        "duration": "90min",
        "venue": "Štadión FC Petržalka",
        "address": "Petržalka, Bratislava",
        "city": "Bratislava",
        "photo_url": photo_url,
        "min_price": 0.0,
        "max_price": 0.0,
        "page_free": True,
    }


def _pick_photo(opponent: str, index: int) -> str:
    """Return photo URL — opponent-specific if available, else cycle through generics."""
    opp_lower = opponent.lower()
    for keyword, filename in OPPONENT_PHOTOS.items():
        if keyword in opp_lower:
            return f"{GITHUB_RAW}/{filename}"
    # Cycle through generic photos based on match index
    filename = GENERIC_PHOTOS[index % len(GENERIC_PHOTOS)]
    return f"{GITHUB_RAW}/{filename}"
