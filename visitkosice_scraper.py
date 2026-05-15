#!/usr/bin/env python3
"""
VisitKosice.org event scraper.
Scrapes aktualne-podujatia, filters FREE events only ("Vstup voľný").
Handles date ranges (26.05 - 27.05.2026) → creates separate entry per day.
"""

import logging
import re
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://visitkosice.org"
LISTING_URL = "https://visitkosice.org/podujatia/kategorie/aktualne-podujatia"
MAX_PAGES = 10   # up to ~100 events per run
CRAWL_DELAY = 0.8

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WoevaEventsBot/1.0; +https://woeva.com)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.5",
}

FREE_KEYWORDS = ["vstup voľný", "vstup volny", "vstup je voľný", "zdarma"]


def is_free(vstupne: str) -> bool:
    return any(k in vstupne.lower() for k in FREE_KEYWORDS)


def parse_dates(date_str: str) -> list[str]:
    """
    Parse date string into list of ISO date strings.
    Handles:
      - "16.05.2026"           → ["2026-05-16"]
      - "26.05 - 27.05.2026"  → ["2026-05-26", "2026-05-27"]
      - "26.05 - 28.05.2026"  → ["2026-05-26", "2026-05-27", "2026-05-28"]
    """
    date_str = date_str.strip()

    # Single full date
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", date_str)
    if m:
        return [f"{m.group(3)}-{m.group(2)}-{m.group(1)}"]

    # Range: DD.MM - DD.MM.YYYY or DD.MM.YYYY - DD.MM.YYYY
    m = re.search(r"(\d{2})\.(\d{2})(?:\.\d{4})?\s*[-–]\s*(\d{2})\.(\d{2})\.(\d{4})", date_str)
    if m:
        d1, mo1, d2, mo2, year = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        from datetime import date, timedelta
        start = date(int(year), int(mo1), int(d1))
        end = date(int(year), int(mo2), int(d2))
        results = []
        cur = start
        while cur <= end:
            results.append(cur.isoformat())
            cur += timedelta(days=1)
        return results

    return []


def parse_image_url(src: str) -> str | None:
    """Extract original image URL from /_ipx/... proxy URLs."""
    if not src:
        return None
    m = re.search(r"/_ipx/[^/]+/(https?://.*)", src)
    if m:
        return m.group(1)
    if src.startswith("http"):
        return src
    return BASE_URL + src


def scrape_page(page: int) -> list[dict]:
    url = LISTING_URL if page == 1 else f"{LISTING_URL}?page={page}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"Page {page} fetch failed: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.find_all("div", class_="card-hover")

    events = []
    for card in cards:
        text = card.get_text(separator="|", strip=True)
        parts = [p.strip() for p in text.split("|") if p.strip()]

        title_el = card.find("h3")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            continue

        # Description
        desc_el = card.find(class_="wysiwyg-content")
        description = desc_el.get_text(strip=True) if desc_el else ""

        # Location
        loc_el = card.find("a", href=re.compile(r"maps|location|miesto", re.I))
        if not loc_el:
            # fallback: look for location text pattern
            for el in card.find_all(string=re.compile(r"Košice|ulica|nám|park|hrad", re.I)):
                location_text = el.strip()
                if len(location_text) > 5:
                    break
            else:
                location_text = "Košice"
        else:
            location_text = loc_el.get_text(strip=True)
        # Strip redundant city/country from address (e.g. "Venue, Košice, Slovensko" → "Venue")
        _STRIP = {"košice", "slovensko", "slovakia", "sk"}
        _parts = [p.strip() for p in location_text.split(",")]
        _clean = [p for p in _parts if p.lower() not in _STRIP]
        location_text = _clean[0] if _clean else "Košice"

        # Date
        date_raw = ""
        for i, p in enumerate(parts):
            if p == "Dátum" and i + 1 < len(parts):
                date_raw = parts[i + 1]
                break

        # Time
        time_raw = ""
        for i, p in enumerate(parts):
            if p == "Čas" and i + 1 < len(parts):
                time_raw = parts[i + 1]
                break

        # Vstupné
        vstupne = ""
        for i, p in enumerate(parts):
            if p == "Vstupné od" and i + 1 < len(parts):
                vstupne = parts[i + 1]
                break

        if not is_free(vstupne):
            continue

        # Image — try multiple selectors
        img_el = (
            card.find("img", class_="card-image")
            or card.find("img", class_="detail-img")
            or card.find("img", class_="img-fluid")
            or card.find("img")
        )
        image_url = parse_image_url(img_el.get("src") or img_el.get("data-src") or "") if img_el else None

        # Source URL (link wrapping the card)
        link_el = card.find_parent("a") or card.find("a", href=re.compile(r"/podujatia/"))
        source_url = (BASE_URL + link_el["href"]) if link_el and link_el.get("href") else url

        # Expand date ranges
        dates = parse_dates(date_raw)
        if not dates:
            logger.debug(f"Could not parse date '{date_raw}' for '{title}' — skipping")
            continue

        # Only one entry per event (start date) to avoid duplicates for multi-day events
        iso_date = dates[0]
        events.append({
            "title": title,
            "original_description": description,
            "date": iso_date,
            "time_start": time_raw or "",
            "duration": "",
            "venue": location_text,
            "address": "",
            "city": "Košice",
            "country": "SK",
            "photo_url": image_url,
            "source_url": source_url,
            "is_free": True,
            "min_price": 0.0,
            "price_raw": "Vstup voľný",
            "source": "visitkosice",
        })

    return events


def scrape_visitkosice(existing_urls: set) -> list[dict]:
    """Return list of new free events from visitkosice.org not yet in DB."""
    all_events = []
    empty_pages = 0

    for page in range(1, MAX_PAGES + 1):
        logger.info(f"VisitKošice: scraping page {page}")
        events = scrape_page(page)

        if not events and page > 1:
            empty_pages += 1
            if empty_pages >= 2:
                logger.info("2 empty pages in a row — stopping")
                break

        new = [e for e in events if e["source_url"] not in existing_urls]
        for e in new:
            existing_urls.add(e["source_url"])  # prevent same event appearing on multiple pages
        all_events.extend(new)
        logger.info(f"  Page {page}: {len(events)} free events, {len(new)} new")

        if len(events) < 10:
            break  # last page

        time.sleep(CRAWL_DELAY)

    logger.info(f"VisitKošice total new events: {len(all_events)}")
    return all_events
