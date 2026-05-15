#!/usr/bin/env python3
"""
Eventbrite London free events scraper.
Scrapes eventbrite.com/d/united-kingdom--london/free--events/
Parses JSON-LD from event detail pages.
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

BASE_URL = "https://www.eventbrite.com"
LISTING_CONFIGS = [
    {"url": "https://www.eventbrite.com/d/united-kingdom--london/free--events/", "city": "London", "country": "GB"},
    {"url": "https://www.eventbrite.com/d/austria--wien/free--music--events/", "city": "Vienna", "country": "AT"},
]
MAX_PAGES = 2
CRAWL_DELAY = 0.8

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Eventbrite event URL: /e/{slug}-{id}/
EVENT_URL_RE = re.compile(r"^https?://www\.eventbrite\.com/e/[^/?#]+-\d+/?$", re.I)


def scrape_eventbrite(existing_urls: set) -> list[dict]:
    """Return list of new free events from Eventbrite (London + Vienna)."""
    events: list[dict] = []
    seen: set[str] = set()

    for config in LISTING_CONFIGS:
        listing_url = config["url"]
        city = config["city"]
        country = config["country"]

        for page in range(1, MAX_PAGES + 1):
            url = listing_url if page == 1 else f"{listing_url}?page={page}"
            logger.info(f"Eventbrite {city}: fetching page {page}")
            r = _fetch(url)
            if r is None:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            found = _extract_event_urls(soup)
            logger.info(f"  Found {len(found)} event links")
            if not found:
                break

            for event_url in found:
                if event_url in seen or event_url in existing_urls:
                    continue
                seen.add(event_url)
                time.sleep(CRAWL_DELAY)
                event = _scrape_event_detail(event_url, city=city, country=country)
                if not event:
                    continue
                if _is_past(event.get("date", "")):
                    continue
                logger.info(f"Eventbrite {city}: {event['title'][:60]}")
                events.append(event)
                existing_urls.add(event_url)

            time.sleep(CRAWL_DELAY)

    logger.info(f"Eventbrite total new events: {len(events)}")
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
        href = a["href"].split("?")[0].rstrip("/")
        if not href.startswith("http"):
            href = urljoin(BASE_URL, href)
        if EVENT_URL_RE.match(href) and href not in seen:
            seen.add(href)
            urls.append(href)
    return urls


def _scrape_event_detail(url: str, city: str = "London", country: str = "GB") -> dict | None:
    r = _fetch(url)
    if r is None:
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    # Try JSON-LD first
    jsonld = _find_jsonld_event(soup)

    # Try Next.js __NEXT_DATA__ as fallback
    next_data = _find_next_data(soup)

    # --- Title ---
    title = ""
    if jsonld:
        title = (jsonld.get("name") or "").strip()
    if not title and next_data:
        title = _deep_get(next_data, "props", "pageProps", "event", "name") or ""
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""
    if not title:
        return None

    # --- Price check (listing is already /free--events/ but verify) ---
    is_free = True
    if jsonld and jsonld.get("offers"):
        offers = jsonld["offers"]
        if isinstance(offers, dict):
            offers = [offers]
        for offer in (offers or []):
            price = offer.get("price", "0")
            try:
                if float(price) > 0:
                    is_free = False
                    break
            except (ValueError, TypeError):
                pass
    if not is_free:
        return None

    # --- Date + Time ---
    start_date = ""
    time_start = ""
    if jsonld and jsonld.get("startDate"):
        start_date = jsonld["startDate"][:10]
        time_start = _parse_iso_time(jsonld["startDate"])

    # --- Description ---
    description = ""
    if jsonld and jsonld.get("description"):
        description = jsonld["description"].strip()[:1000]
    if not description:
        desc_div = soup.find("div", class_=re.compile(r"description|summary", re.I))
        if desc_div:
            description = desc_div.get_text(" ", strip=True)[:1000]

    # --- Image ---
    image = ""
    og = soup.find("meta", property="og:image")
    if og:
        image = og.get("content", "").strip()
    if not image and jsonld and jsonld.get("image"):
        img = jsonld["image"]
        image = img if isinstance(img, str) else (img[0] if img else "")

    # --- Venue ---
    venue = ""
    address = ""
    if jsonld and isinstance(jsonld.get("location"), dict):
        loc = jsonld["location"]
        venue = (loc.get("name") or "").strip()
        addr_obj = loc.get("address", {})
        if isinstance(addr_obj, dict):
            parts = [
                addr_obj.get("streetAddress", ""),
                addr_obj.get("addressLocality", ""),
                addr_obj.get("postalCode", ""),
            ]
            address = ", ".join(p for p in parts if p)
        elif isinstance(addr_obj, str):
            address = addr_obj

    return {
        "source_url": url,
        "source": "eventbrite",
        "title": title,
        "original_description": description,
        "date": start_date,
        "time_start": time_start,
        "duration": "",
        "venue": venue,
        "address": address,
        "city": city,
        "country": country,
        "photo_url": image,
        "min_price": 0.0,
        "price_raw": "Free",
        "is_free": True,
        "price": 0.0,
    }


def _find_jsonld_event(soup: BeautifulSoup) -> dict | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = script.string
            if not raw:
                continue
            data = json.loads(raw)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "Event":
                        return item
            if isinstance(data, dict):
                if data.get("@type") == "Event":
                    return data
                for item in data.get("@graph", []):
                    if item.get("@type") == "Event":
                        return item
        except Exception as e:
            logger.debug(f"JSON-LD parse error: {e}")
    return None


def _find_next_data(soup: BeautifulSoup) -> dict | None:
    script = soup.find("script", id="__NEXT_DATA__")
    if script and script.string:
        try:
            return json.loads(script.string)
        except Exception:
            pass
    return None


def _deep_get(obj: dict, *keys) -> str:
    for key in keys:
        if not isinstance(obj, dict):
            return ""
        obj = obj.get(key, {})
    return obj if isinstance(obj, str) else ""


def _parse_iso_time(date_str: str) -> str:
    try:
        if "T" in date_str:
            time_part = date_str.split("T")[1][:5]
            return time_part if ":" in time_part else ""
        return ""
    except Exception:
        return ""


def _is_past(date_str: str) -> bool:
    if not date_str:
        return False
    try:
        event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = datetime.now().date()
        return (today - event_date).days > 2
    except Exception:
        return False
