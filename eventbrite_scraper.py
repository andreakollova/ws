#!/usr/bin/env python3
"""
Eventbrite London free events scraper.
Extracts event data directly from listing page JSON-LD (ItemList) —
avoids detail page fetches that are blocked on cloud IPs.
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
    {"url": "https://www.eventbrite.com/d/united-kingdom--london/free--music--events/", "city": "London", "country": "GB"},
    {"url": "https://www.eventbrite.com/d/austria--wien/free--music--events/", "city": "Vienna", "country": "AT"},
]
MAX_PAGES = 3
CRAWL_DELAY = 1.2

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Eventbrite event URL: /e/{slug}-{id}/
EVENT_URL_RE = re.compile(r"^https?://(?:www\.)?eventbrite\.(?:com|co\.uk)/e/[^/?#]+-\d+/?$", re.I)


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

            # Try to extract full event data from listing page JSON-LD
            page_events = _extract_from_listing(soup, city=city, country=country,
                                                 existing_urls=existing_urls, seen=seen)
            logger.info(f"  Found {len(page_events)} new events from listing page")

            if not page_events:
                # If we got 0 new events on this page, stop paginating
                break

            events.extend(page_events)
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


def _extract_card_images(soup: BeautifulSoup) -> dict[str, str]:
    """Extract {normalized_event_url: image_url} from listing page HTML cards."""
    result: dict[str, str] = {}
    # Each card is an <a> wrapping the event — find all event links
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0].rstrip("/")
        if not href.startswith("http"):
            href = urljoin(BASE_URL, href)
        if not EVENT_URL_RE.match(href):
            continue
        # Look for image inside the card link or nearby container
        img = a.find("img")
        if not img:
            parent = a.find_parent()
            if parent:
                img = parent.find("img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            # Skip placeholder SVG/logos
            if src and "logo" not in src.lower() and not src.lower().endswith(".svg"):
                result[href] = src
    return result


def _extract_from_listing(soup: BeautifulSoup, city: str, country: str,
                           existing_urls: set, seen: set) -> list[dict]:
    """Extract events from listing page JSON-LD ItemList (no detail page fetches needed)."""
    events: list[dict] = []

    # Build URL→image map from HTML cards (more reliable than JSON-LD for images)
    card_images = _extract_card_images(soup)
    logger.info(f"  Found {len(card_images)} card images from HTML")

    # Find all JSON-LD scripts
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = script.string
            if not raw:
                continue
            data = json.loads(raw)

            items_to_check = []
            if isinstance(data, list):
                items_to_check = data
            elif isinstance(data, dict):
                items_to_check = [data]

            for obj in items_to_check:
                if not isinstance(obj, dict):
                    continue

                if obj.get("@type") == "ItemList":
                    list_items = obj.get("itemListElement", [])
                    logger.info(f"  Found ItemList with {len(list_items)} items")
                    for list_item in list_items:
                        item = list_item.get("item", {}) if isinstance(list_item, dict) else {}
                        event = _parse_listing_item(item, city=city, country=country)
                        if not event:
                            continue
                        # Override image with HTML card image (avoids Eventbrite logo)
                        card_img = card_images.get(event["source_url"], "")
                        if card_img:
                            event["photo_url"] = card_img
                        if _should_include(event, existing_urls, seen):
                            events.append(event)
                            existing_urls.add(event["source_url"])
                            seen.add(event["source_url"])

        except Exception as e:
            logger.debug(f"JSON-LD parse error: {e}")

    # Fallback: if no ItemList found, try extracting URLs from page and do targeted detail fetches
    if not events:
        logger.info("  No ItemList found — falling back to URL extraction")
        urls = _extract_event_urls(soup)
        for event_url in urls:
            if event_url in seen or event_url in existing_urls:
                continue
            seen.add(event_url)
            time.sleep(0.8)
            event = _scrape_event_detail(event_url, city=city, country=country)
            if not event:
                continue
            if _is_past(event.get("date", "")):
                continue
            events.append(event)
            existing_urls.add(event_url)

    return events


def _parse_listing_item(item: dict, city: str, country: str) -> dict | None:
    """Parse a single ListItem.item dict from Eventbrite's JSON-LD ItemList."""
    if not item:
        return None

    url = item.get("url", "")
    if not url:
        return None
    # Normalize URL
    url = url.split("?")[0].rstrip("/")
    if not EVENT_URL_RE.match(url):
        return None

    title = (item.get("name") or "").strip()
    if not title:
        return None

    # Price check — free events
    offers = item.get("offers", [])
    if isinstance(offers, dict):
        offers = [offers]
    for offer in (offers or []):
        try:
            if float(offer.get("price", "0")) > 0:
                return None
        except (ValueError, TypeError):
            pass

    # Date + time
    start_date = ""
    time_start = ""
    raw_start = item.get("startDate", "")
    if raw_start:
        start_date = raw_start[:10]
        time_start = _parse_iso_time(raw_start)

    if _is_past(start_date):
        return None

    description = (item.get("description") or "").strip()[:1000]

    # Image
    image = ""
    img_val = item.get("image", "")
    if isinstance(img_val, list):
        img_val = img_val[0] if img_val else ""
    if isinstance(img_val, dict):
        img_val = img_val.get("url", "")
    image = (img_val or "").strip()

    # Location
    venue = ""
    address = ""
    loc = item.get("location", {})
    if isinstance(loc, dict):
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


def _should_include(event: dict, existing_urls: set, seen: set) -> bool:
    url = event["source_url"]
    if url in seen or url in existing_urls:
        return False
    if _is_past(event.get("date", "")):
        return False
    return True


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
    """Fallback: scrape a single event detail page."""
    r = _fetch(url)
    if r is None:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    jsonld = _find_jsonld_event(soup)

    title = ""
    if jsonld:
        title = (jsonld.get("name") or "").strip()
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""
    if not title:
        return None

    is_free = True
    if jsonld and jsonld.get("offers"):
        offers = jsonld["offers"]
        if isinstance(offers, dict):
            offers = [offers]
        for offer in (offers or []):
            try:
                if float(offer.get("price", "0")) > 0:
                    is_free = False
                    break
            except (ValueError, TypeError):
                pass
    if not is_free:
        return None

    start_date = ""
    time_start = ""
    if jsonld and jsonld.get("startDate"):
        start_date = jsonld["startDate"][:10]
        time_start = _parse_iso_time(jsonld["startDate"])

    description = ""
    if jsonld and jsonld.get("description"):
        description = jsonld["description"].strip()[:1000]

    image = ""
    og = soup.find("meta", property="og:image")
    if og:
        image = og.get("content", "").strip()
    if not image and jsonld and jsonld.get("image"):
        img = jsonld["image"]
        image = img if isinstance(img, str) else (img[0] if img else "")

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
