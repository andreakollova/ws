#!/usr/bin/env python3
"""
GoOut.net Slovakia event scraper.
Scrapes public event listing pages, filters FREE events only.
Respects robots.txt: no disallowed paths, polite crawl delay.
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
    "https://goout.net/sk/slovensko/akcie/leztaofti/",
]
BASE_URL = "https://goout.net"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WoevaEventsBot/1.0; +https://woeva.com)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.5",
}
MAX_EVENTS = 20
CRAWL_DELAY = 1.5

# GoOut schedule URL pattern for Slovak site
SCHEDULE_RE = re.compile(r"^https?://goout\.net/sk/[^/]+/schedule:\d+/?", re.I)


def scrape_goout(existing_urls: set) -> list[dict]:
    """Return list of raw free event dicts scraped from GoOut Slovakia."""
    events = []
    all_event_urls: list[str] = []

    for listing_url in LISTING_URLS:
        logger.info(f"Fetching GoOut listing: {listing_url}")
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

        if _is_free(event):
            logger.info(f"Free event found: {event['title'][:60]}")
            events.append(event)
            existing_urls.add(url)
        else:
            logger.debug(f"Not free (price={event.get('min_price')}): {event['title'][:40]}")

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
        if SCHEDULE_RE.match(href) and href not in seen:
            seen.add(href)
            urls.append(href)
    return urls


def _scrape_event_detail(url: str) -> dict | None:
    r = _fetch(url)
    if r is None:
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    # Try JSON-LD (Schema.org Event) first — cleanest source
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = script.string
            if not raw:
                continue
            data = json.loads(raw)
            if isinstance(data, list):
                data = next((d for d in data if d.get("@type") == "Event"), None)
            if data and data.get("@type") == "Event":
                return _parse_jsonld_event(data, url)
        except Exception as e:
            logger.debug(f"JSON-LD parse error for {url}: {e}")

    # Fallback: basic HTML extraction
    return _parse_html_fallback(soup, url)


def _parse_jsonld_event(data: dict, url: str) -> dict:
    # Price
    offers = data.get("offers", {})
    if isinstance(offers, list):
        offers = min(offers, key=lambda o: float(o.get("lowPrice", o.get("price", 999)) or 0))
    low_price = offers.get("lowPrice", offers.get("price", ""))
    try:
        min_price_val = float(low_price) if low_price not in (None, "") else 999.0
    except (TypeError, ValueError):
        min_price_val = 999.0

    # Location
    location = data.get("location", {})
    venue_name = location.get("name", "")
    address_obj = location.get("address", {})
    if isinstance(address_obj, str):
        address_str = address_obj
        city = ""
    else:
        parts = [
            address_obj.get("streetAddress", ""),
            address_obj.get("addressLocality", ""),
        ]
        address_str = ", ".join(p for p in parts if p)
        city = address_obj.get("addressLocality", "")

    # Dates
    start = data.get("startDate", "")
    end = data.get("endDate", "")

    # Image
    image = data.get("image", "")
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url", "")

    return {
        "source_url": url,
        "source": "goout",
        "title": data.get("name", "").strip(),
        "original_description": (data.get("description") or "").strip()[:1000],
        "date": start[:10] if start else "",
        "time_start": start[11:16] if len(start) >= 16 else "",
        "duration": _calc_duration(start, end),
        "venue": venue_name,
        "address": address_str,
        "city": city,
        "photo_url": image,
        "min_price": min_price_val,
    }


def _parse_html_fallback(soup: BeautifulSoup, url: str) -> dict | None:
    title_el = soup.find("h1") or soup.find("h2")
    title = title_el.get_text(strip=True) if title_el else ""
    if not title:
        og = soup.find("meta", property="og:title")
        title = og.get("content", "").strip() if og else ""
    if not title:
        return None

    og_img = soup.find("meta", property="og:image")
    image = og_img.get("content", "").strip() if og_img else ""

    return {
        "source_url": url,
        "source": "goout",
        "title": title,
        "original_description": "",
        "date": "",
        "time_start": "",
        "duration": "",
        "venue": "",
        "address": "",
        "city": "",
        "photo_url": image,
        "min_price": 999.0,  # Unknown — will be filtered out
    }


def _is_free(event: dict) -> bool:
    return event.get("min_price", 999) == 0.0


def _calc_duration(start: str, end: str) -> str:
    if not start or not end:
        return ""
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
        diff = e - s
        if diff.total_seconds() <= 0:
            return ""
        total_minutes = int(diff.total_seconds() // 60)
        hours = total_minutes // 60
        minutes = total_minutes % 60
        if hours > 0 and minutes > 0:
            return f"{hours}h {minutes}min"
        if hours > 0:
            return f"{hours}h"
        return f"{minutes}min"
    except Exception:
        return ""
