#!/usr/bin/env python3
"""
Stará tržnica Bratislava — farmárska tržnica scraper.
Scrapes the weekly Trh-Piac-Markt Saturday markets from staratrznica.sk/sk/program.
"""

import logging
import re
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://staratrznica.sk"
PROGRAM_URL = "https://staratrznica.sk/sk/program"
HEADERS = {
    "User-Agent": "WoevaBot/1.0 (+https://woeva.app)",
    "Accept-Language": "sk-SK,sk;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}
MAX_EVENTS = 20

# Match event slugs we care about — Saturday markets
TRH_KEYWORDS = ["trh-piac", "farmársk", "farmarsk", "trznica", "tržnica", "trh"]


def _is_trh(slug: str, title: str) -> bool:
    slug_low = slug.lower()
    title_low = title.lower()
    return any(k in slug_low or k in title_low for k in TRH_KEYWORDS)


def scrape_staratrznica(existing_urls: set) -> list[dict]:
    logger.info(f"Fetching Stará tržnica program: {PROGRAM_URL}")
    try:
        res = requests.get(PROGRAM_URL, headers=HEADERS, timeout=20)
        res.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Stará tržnica fetch failed: {e}")
        return []

    soup = BeautifulSoup(res.text, "html.parser")

    # Links follow pattern: /sk/program/<slug>/<YYYY-MM-DD>
    event_link_re = re.compile(r"^/sk/program/([^/]+)/(\d{4}-\d{2}-\d{2})$")
    seen_urls: set[str] = set()
    events: list[dict] = []
    today = datetime.now(timezone.utc).date()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = event_link_re.match(href)
        if not m:
            continue
        slug, date_str = m.group(1), m.group(2)

        # Skip past events
        try:
            event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if event_date < today:
            continue

        source_url = BASE_URL + href
        if source_url in existing_urls or source_url in seen_urls:
            continue

        # Find title from the link text or nearby heading
        title_raw = a.get_text(separator=" ", strip=True)
        if not title_raw:
            heading = a.find(["h2", "h3", "h4", "strong"])
            if heading:
                title_raw = heading.get_text(strip=True)
        if not title_raw:
            title_raw = slug.replace("-", " ").title()

        if not _is_trh(slug, title_raw):
            continue

        seen_urls.add(source_url)

        # Try to find a cover image near this link
        photo_url = None
        parent = a.parent
        for _ in range(5):
            if parent is None:
                break
            img = parent.find("img")
            if img and img.get("src"):
                src = img["src"]
                if not src.startswith("http"):
                    src = BASE_URL + src
                photo_url = src
                break
            parent = parent.parent

        events.append({
            "title": "Farmárska tržnica – Trh-Piac-Markt",
            "description": (
                "Každú sobotu na Námestí SNP prídu farmári, pestovatelia a remeselníci "
                "priamo z vidieka. Čerstvé ovocie, zelenina, syry, mäso, domáce produkty "
                "a remeselné výrobky. Vstup voľný."
            ),
            "date": date_str,
            "time_start": "09:00",
            "duration": "6h",
            "venue": "Stará tržnica",
            "address": "Námestie SNP 25, Bratislava",
            "city": "Bratislava",
            "country": "SK",
            "tag": "trhy",
            "photo_url": photo_url or "",
            "source_url": source_url,
            "source": "staratrznica",
            "is_recurring": True,
        })
        logger.info(f"Stará tržnica: found market on {date_str}")
        time.sleep(0.3)

        if len(events) >= MAX_EVENTS:
            break

    logger.info(f"Stará tržnica: {len(events)} new market events")
    return events
