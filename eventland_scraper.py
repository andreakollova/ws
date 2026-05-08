#!/usr/bin/env python3
"""
Eventland.eu Slovakia event scraper.
Scrapes the /zadarmo/ (free) listing pages directly — no paid-event filtering needed.
Respects polite crawl delay.
"""

import json
import logging
import re
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

LISTING_URLS = [
    "https://eventland.eu/sk/bratislava-sk/zadarmo/",
    "https://eventland.eu/sk/kosice-sk/zadarmo/",
]
BASE_URL = "https://eventland.eu"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WoevaEventsBot/1.0; +https://woeva.com)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.5",
}
MAX_EVENTS = 20
CRAWL_DELAY = 1.5

# Eventland event URL: /sk/{city}/event/{id}/{slug}/
EVENT_URL_RE = re.compile(
    r"^https?://eventland\.eu/sk/[^/]+/event/\d+/[^/]+/?$", re.I
)

# City slug → display name
CITY_MAP = {
    "bratislava-sk": "Bratislava",
    "kosice-sk": "Košice",
}


def scrape_eventland(existing_urls: set) -> list[dict]:
    """Return list of raw free event dicts scraped from Eventland Slovakia."""
    events = []
    all_event_urls: list[str] = []

    for listing_url in LISTING_URLS:
        logger.info(f"Fetching Eventland listing: {listing_url}")
        r = _fetch(listing_url)
        if r is None:
            logger.error(f"Failed to fetch: {listing_url}")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        found = _extract_event_urls(soup)
        logger.info(f"  Found {len(found)} event links")
        all_event_urls.extend(found)

    seen_urls: set[str] = set()
    for url in all_event_urls:
        if url in seen_urls:
            continue
        seen_urls.add(url)

        if url in existing_urls:
            logger.debug(f"Skip (known): {url}")
            continue

        time.sleep(CRAWL_DELAY)
        event = _scrape_event_detail(url)
        if not event:
            continue

        if not _is_free(event):
            logger.debug(f"Not free (price={event.get('price_raw')}): {event['title'][:40]}")
            continue

        if _is_past(event.get("date", "")):
            logger.debug(f"Skip past event ({event['date']}): {event['title'][:40]}")
            continue

        logger.info(f"Free event found: {event['title'][:60]}")
        events.append(event)
        existing_urls.add(url)

        if len(events) >= MAX_EVENTS:
            break

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


def _extract_event_urls(soup: BeautifulSoup) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            href = urljoin(BASE_URL, href)
        href = href.split("?")[0].rstrip("/")
        if EVENT_URL_RE.match(href) and href not in seen:
            seen.add(href)
            urls.append(href)
    return urls


def _scrape_event_detail(url: str) -> dict | None:
    r = _fetch(url)
    if r is None:
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    # --- Price from ul.item-listing--data (first occurrence = main event) ---
    price_raw = ""
    data_ul = soup.find("ul", class_="item-listing--data")
    if data_ul:
        items = [li.get_text(strip=True) for li in data_ul.find_all("li")]
        price_raw = items[2] if len(items) > 2 else ""

    # --- JSON-LD Event ---
    jsonld = _find_jsonld_event(soup)

    # --- Title ---
    title = ""
    if jsonld:
        title = (jsonld.get("name") or "").strip()
    if not title:
        h2 = soup.find("h2", class_="item-listing--title")
        title = h2.get_text(strip=True) if h2 else ""
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""
    if not title:
        return None

    # --- Date ---
    start_date = ""
    if jsonld and jsonld.get("startDate"):
        start_date = _parse_iso_date(jsonld["startDate"])
    if not start_date and data_ul and items:
        start_date = _parse_sk_date(items[0])

    # --- Description (full text from div.item-listing--description) ---
    description = ""
    desc_div = soup.find("div", class_="item-listing--description")
    if desc_div:
        description = desc_div.get_text(" ", strip=True)[:1000]
    elif jsonld and jsonld.get("description"):
        description = jsonld["description"].strip()[:1000]

    # --- Image ---
    # og:image is event-specific; JSON-LD image is often a generic category placeholder
    og = soup.find("meta", property="og:image")
    image = og.get("content", "").strip() if og else ""
    if not image and jsonld and jsonld.get("image"):
        image = jsonld["image"] if isinstance(jsonld["image"], str) else (jsonld["image"] or [None])[0] or ""

    # --- Venue / Address / City ---
    venue = ""
    address = ""
    city = _city_from_url(url)

    if jsonld and isinstance(jsonld.get("location"), dict):
        loc = jsonld["location"]
        addr_obj = loc.get("address", {})
        if isinstance(addr_obj, dict):
            addr_name = addr_obj.get("name", "")
            # "TK SLÁVIA STU BRATISLAVA, Májová 21, Petržalka, Slovensko"
            parts = [p.strip() for p in addr_name.split(",")]
            venue = parts[0] if parts else ""
            address = ", ".join(parts[:-1]) if len(parts) > 1 else addr_name
        elif isinstance(addr_obj, str):
            address = addr_obj

    return {
        "source_url": url,
        "source": "eventland",
        "title": title,
        "original_description": description,
        "date": start_date,
        "time_start": "",
        "duration": "",
        "venue": venue,
        "address": address,
        "city": city,
        "photo_url": image,
        "min_price": 0.0 if not price_raw or "€" not in price_raw else 999.0,
        "price_raw": price_raw,
    }


def _find_jsonld_event(soup: BeautifulSoup) -> dict | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = script.string
            if not raw:
                continue
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("@graph"):
                for item in data["@graph"]:
                    if item.get("@type") == "Event":
                        return item
            if isinstance(data, dict) and data.get("@type") == "Event":
                return data
        except Exception as e:
            logger.debug(f"JSON-LD parse error: {e}")
    return None


def _parse_iso_date(date_str: str) -> str:
    """'2026-05-23' or '2026-05-23T14:00' → '2026-05-23'"""
    try:
        return date_str[:10]
    except Exception:
        return ""


def _parse_sk_date(date_str: str) -> str:
    """'23.05.2026' or '23.05.2026 - 25.05.2026' → '2026-05-23'"""
    # Take only the start date if it's a range
    part = date_str.split("-")[0].strip() if " - " in date_str else date_str.strip()
    # Handle "29.05.2026 - 31.05.2026" — split on " - " first
    part = date_str.split(" - ")[0].strip()
    try:
        dt = datetime.strptime(part, "%d.%m.%Y")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


def _city_from_url(url: str) -> str:
    """Extract city name from Eventland URL slug."""
    m = re.search(r"/sk/([^/]+)/event/", url)
    if m:
        return CITY_MAP.get(m.group(1), m.group(1).replace("-sk", "").capitalize())
    return ""


def _is_free(event: dict) -> bool:
    """Free if price_raw has no euro sign (listing is already /zadarmo/ but verify)."""
    price_raw = event.get("price_raw", "")
    return "€" not in price_raw


def _is_past(date_str: str) -> bool:
    """Return True if event date is more than 2 days in the past."""
    if not date_str:
        return False
    try:
        event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = datetime.now().date()
        return (today - event_date).days > 2
    except Exception:
        return False
