#!/usr/bin/env python3
"""
Nitra.eu calendar scraper — nitra.eu/kalendar
Scrapes all 16 listing pages, detects free events from text, returns new events.
"""

import logging
import re
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://www.nitra.eu"
LISTING_BASE = "https://www.nitra.eu/kalendar"
MAX_PAGES = 16
CRAWL_DELAY = 1.0

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.5",
}

# Matches /kalendar/{id}/{slug}
EVENT_URL_RE = re.compile(r"^https?://(?:www\.)?nitra\.eu/kalendar/\d+/[^/?#]+/?$", re.I)

# Free signals in Slovak/Czech text
FREE_RE = re.compile(
    r"\b(vstup\s+zdarma|vstupn[eé]\s+zdarma|vstup\s+voln[yý]|zadarmo|voln[yý]\s+vstup|free\s+vstup|bezplatn[yý]|free)\b",
    re.I,
)

# Paid signals — number + currency or ticket keywords
PAID_RE = re.compile(
    r"(\b[1-9]\d*\s*(€|eur\b)|vstupn[eé]\s*:\s*\d|l[ií]stk[yi]|vstupenk[yi]|vstupn[eé]\s+\d)",
    re.I,
)

# Date patterns
# "11.6.2026 17:00 - 18:00"  or  "11. - 13.6.2026"  or  "11.6.2026"
DATE_SIMPLE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
DATE_TIME_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{2}):(\d{2})")
DATE_RANGE_SHORT_RE = re.compile(r"(\d{1,2})\.\s*-\s*(\d{1,2})\.(\d{1,2})\.(\d{4})")


def scrape_nitra(existing_urls: set) -> list[dict]:
    """Return list of new free events from nitra.eu/kalendar."""
    events: list[dict] = []
    seen: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = LISTING_BASE if page == 1 else f"{LISTING_BASE}/{page}"
        logger.info(f"Nitra.eu: fetching page {page} — {url}")
        r = _fetch(url)
        if r is None:
            logger.warning(f"Nitra.eu page {page} failed, stopping")
            break

        soup = BeautifulSoup(r.text, "html.parser")
        event_links = _extract_event_links(soup)
        logger.info(f"  Found {len(event_links)} event links on page {page}")

        if not event_links:
            logger.info(f"  No events on page {page}, stopping pagination")
            break

        new_on_page = 0
        for event_url in event_links:
            if event_url in seen or event_url in existing_urls:
                continue
            seen.add(event_url)
            new_on_page += 1

            time.sleep(CRAWL_DELAY)
            event = _scrape_event_detail(event_url)
            if not event:
                continue

            if _is_past(event.get("date", "")):
                logger.debug(f"Skip past: {event['title'][:50]}")
                continue

            if not event.get("is_free"):
                logger.debug(f"Skip paid: {event['title'][:50]}")
                continue

            logger.info(f"  Free event: {event['title'][:60]}")
            events.append(event)
            existing_urls.add(event_url)

        if new_on_page == 0:
            logger.info(f"  All events on page {page} already known, stopping")
            break

    logger.info(f"Nitra.eu total new free events: {len(events)}")
    return events


def _fetch(url: str, retries: int = 3) -> requests.Response | None:
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            logger.warning(f"Fetch attempt {attempt + 1}/{retries} failed for {url}: {e}")
            time.sleep(3)
    return None


def _extract_event_links(soup: BeautifulSoup) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            href = urljoin(BASE_URL, href)
        href = href.split("?")[0].rstrip("/")
        if EVENT_URL_RE.match(href) and href not in seen:
            seen.add(href)
            links.append(href)
    return links


def _scrape_event_detail(url: str) -> dict | None:
    r = _fetch(url)
    if r is None:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    main = soup.find("main") or soup

    # Title
    h1 = main.find("h1")
    title = h1.get_text(strip=True) if h1 else ""
    if not title:
        return None

    # Full text for date/venue extraction and free detection
    full_text = main.get_text(" ", strip=True)

    # Date + time
    start_date, time_start = _parse_date_time(full_text)

    # Venue — after "Miesto:" up to next bullet or line break
    venue = ""
    miesto_m = re.search(r"Miesto\s*:\s*(.+?)(?:\s*Prečítať|\s*•|\s*\n|$)", full_text, re.I)
    if miesto_m:
        venue = miesto_m.group(1).strip()

    # Description — text after "Prečítať" keyword (the "read more" content)
    description = ""
    precitat_m = re.search(r"Prečítať\s+(.*)", full_text, re.I | re.S)
    if precitat_m:
        description = precitat_m.group(1).strip()[:1000]
    else:
        # Fallback: grab all p tags from main
        paras = [p.get_text(" ", strip=True) for p in main.find_all("p")]
        description = " ".join(paras)[:1000]

    # Free detection — check full text + description
    check_text = full_text + " " + description
    is_free = _detect_free(check_text)

    # Image — look for media.moderneobce.sk/data/calendar
    image = ""
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "media.moderneobce.sk/data/calendar" in src or "nitra.eu" in src:
            if not src.startswith("http"):
                src = urljoin(BASE_URL, src)
            image = src
            break
    if not image:
        og = soup.find("meta", property="og:image")
        if og:
            image = og.get("content", "").strip()

    return {
        "source_url": url,
        "source": "nitra",
        "title": title,
        "original_description": description,
        "date": start_date,
        "time_start": time_start,
        "duration": "",
        "venue": venue,
        "address": venue,
        "city": "Nitra",
        "country": "SK",
        "photo_url": image,
        "min_price": 0.0,
        "price_raw": "Vstup voľný" if is_free else "",
        "is_free": is_free,
        "price": 0.0,
    }


def _detect_free(text: str) -> bool:
    """True if event appears free based on text analysis."""
    if PAID_RE.search(text):
        return False
    if FREE_RE.search(text):
        return True
    # No price mentioned → assume free
    return True


def _parse_date_time(text: str) -> tuple[str, str]:
    """Extract (YYYY-MM-DD, HH:MM) from event metadata text."""
    # Try "11.6.2026 17:00"
    m = DATE_TIME_RE.search(text)
    if m:
        day, month, year, hour, minute = m.groups()
        try:
            dt = datetime(int(year), int(month), int(day))
            return dt.strftime("%Y-%m-%d"), f"{int(hour):02d}:{int(minute):02d}"
        except ValueError:
            pass

    # Try "11. - 13.6.2026" range → use start day with end month/year
    m = DATE_RANGE_SHORT_RE.search(text)
    if m:
        day_start, day_end, month, year = m.groups()
        try:
            dt = datetime(int(year), int(month), int(day_start))
            return dt.strftime("%Y-%m-%d"), ""
        except ValueError:
            pass

    # Try simple "11.6.2026"
    m = DATE_SIMPLE_RE.search(text)
    if m:
        day, month, year = m.groups()
        try:
            dt = datetime(int(year), int(month), int(day))
            return dt.strftime("%Y-%m-%d"), ""
        except ValueError:
            pass

    return "", ""


def _is_past(date_str: str) -> bool:
    if not date_str:
        return False
    try:
        event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = datetime.now().date()
        return (today - event_date).days > 2
    except Exception:
        return False
