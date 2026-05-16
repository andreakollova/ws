#!/usr/bin/env python3
"""
Eventland.eu event scraper — Bratislava, Košice, Vienna, Prague.
Scrapes the /zadarmo/ and /free-events/ listing pages (free events only).
Respects polite crawl delay.
"""

import json
import logging
import random
import re
import time
from datetime import datetime, date as _date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

LISTING_URLS = [
    "https://eventland.eu/sk/bratislava-sk/zadarmo/",
]
BASE_URL = "https://eventland.eu"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WoevaEventsBot/1.0; +https://woeva.com)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.5",
}
MAX_EVENTS = 60
MAX_CHECKS_PER_LISTING = 25   # stop after checking this many unknown URLs per listing page
CRAWL_DELAY = 0.8

# Eventland event URL: /{locale?}/{city}/event/{id}/{slug}/
EVENT_URL_RE = re.compile(
    r"^https?://eventland\.eu/(?:[a-z]{2}/)?[^/]+/event/\d+/[^/]+/?$", re.I
)

# Custom photo pool for "Tanec pri fontáne" recurring event.
# Run upload_tanec_photos.py to populate Supabase Storage, then paste the URLs here.
TANEC_PHOTO_URLS: list[str] = [
    # Paste URLs here after running upload_tanec_photos.py
]

# Title keywords that identify the Tanec pri fontáne event (case-insensitive)
TANEC_TITLE_RE = re.compile(r'tanec.{0,10}font', re.I)

# Recurring event detection — matches "Every Wednesday", "Každú stredu", etc.
RECURRING_RE = re.compile(
    r'\b(every|weekly|každ[uúéý]|každom|každé|wöchentlich|jeweils|pravidelne|týždenne|opakujúce)\b',
    re.I,
)

# City slug → display name
CITY_MAP = {
    "bratislava-sk": "Bratislava",
    "kosice-sk": "Košice",
    "vienna-at": "Vienna",
    "vienna": "Vienna",
    "prague-cz": "Prague",
    "prague": "Prague",
}

# Country name strings to strip from addresses
COUNTRY_NAMES = {
    "slovensko", "slovakia", "sk",
    "österreich", "austria", "at",
    "česká republika", "czech republic", "czechia", "cz",
}

# City slug → country (ISO 3166-1 alpha-2)
CITY_COUNTRY = {
    "bratislava-sk": "SK",
    "kosice-sk": "SK",
    "vienna-at": "AT",
    "vienna": "AT",
    "prague-cz": "CZ",
    "prague": "CZ",
}


def scrape_eventland(existing_urls: set) -> list[dict]:
    """Return list of raw free event dicts scraped from Eventland Slovakia."""
    events = []
    seen_urls: set[str] = set()

    for listing_url in LISTING_URLS:
        logger.info(f"Fetching Eventland listing: {listing_url}")
        r = _fetch(listing_url)
        if r is None:
            logger.error(f"Failed to fetch: {listing_url}")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        found = _extract_event_urls(soup)
        logger.info(f"  Found {len(found)} event links")

        checks = 0
        for url, listing_image in found:
            if url in seen_urls:
                continue
            seen_urls.add(url)

            if url in existing_urls:
                logger.debug(f"Skip (known): {url}")
                continue

            if checks >= MAX_CHECKS_PER_LISTING:
                logger.info(f"Eventland: reached MAX_CHECKS_PER_LISTING for {listing_url}")
                break
            checks += 1

            time.sleep(CRAWL_DELAY)
            event = _scrape_event_detail(url, listing_image=listing_image)
            if not event:
                continue

            if not _is_free(event):
                logger.debug(f"Not free (price={event.get('price_raw')}): {event['title'][:40]}")
                continue

            # For recurring events use end date for the past-check; never skip if no date
            check_date = (event.get("recurring_end_date") or event.get("date", "")) if event.get("is_recurring") else event.get("date", "")
            if check_date and _is_past(check_date):
                logger.debug(f"Skip past event ({check_date}): {event['title'][:40]}")
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


def _extract_event_urls(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Return list of (event_url, listing_image_url) tuples."""
    seen: set[str] = set()
    results: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            href = urljoin(BASE_URL, href)
        href = href.split("?")[0].rstrip("/")
        if EVENT_URL_RE.match(href) and href not in seen:
            seen.add(href)
            # Try to grab image from the listing card (inside same <a> or sibling)
            img = a.find("img")
            listing_image = ""
            if img:
                listing_image = img.get("src") or img.get("data-src") or ""
                if listing_image and not listing_image.startswith("http"):
                    listing_image = urljoin(BASE_URL, listing_image)
            results.append((href, listing_image))
    return results


def _scrape_event_detail(url: str, listing_image: str = "") -> dict | None:
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

    # --- Date + Time ---
    start_date = ""
    time_start = ""
    is_recurring = False
    recurring_end_date = ""

    if jsonld and jsonld.get("startDate"):
        start_date = _parse_iso_date(jsonld["startDate"])
        time_start = _parse_iso_time(jsonld["startDate"])

    # endDate in JSON-LD means the event repeats or spans a range
    if jsonld and jsonld.get("endDate"):
        end_iso = _parse_iso_date(jsonld["endDate"])
        if end_iso and end_iso != start_date:
            recurring_end_date = end_iso
            try:
                if start_date and (_date.fromisoformat(end_iso) - _date.fromisoformat(start_date)).days > 14:
                    is_recurring = True
            except Exception:
                pass

    if data_ul and items:
        date_item = items[0]
        if RECURRING_RE.search(date_item):
            is_recurring = True
        elif " - " in date_item:
            # "27.05.2026 - 23.09.2026" style range
            sd, ed = _parse_date_range(date_item)
            if not start_date and sd:
                start_date = sd
            if ed and not recurring_end_date:
                recurring_end_date = ed
            if sd and ed:
                try:
                    if (_date.fromisoformat(ed) - _date.fromisoformat(sd)).days > 14:
                        is_recurring = True
                except Exception:
                    pass
        elif not start_date:
            start_date = _parse_sk_date(date_item)

    # --- Description (full text from div.item-listing--description) ---
    description = ""
    desc_div = soup.find("div", class_="item-listing--description")
    if desc_div:
        description = desc_div.get_text(" ", strip=True)[:1000]
    elif jsonld and jsonld.get("description"):
        description = jsonld["description"].strip()[:1000]

    # --- Image ---
    og = soup.find("meta", property="og:image")
    image = og.get("content", "").strip() if og else ""
    if not image and jsonld and jsonld.get("image"):
        img_val = jsonld["image"] if isinstance(jsonld["image"], str) else (jsonld["image"] or [None])[0] or ""
        # Skip logo/SVG placeholders — eventland puts site logo in JSON-LD for some events
        if img_val and "logo" not in img_val.lower() and not img_val.lower().endswith(".svg"):
            image = img_val
    if not image:
        # Listing page image is guaranteed correct — use it before generic selectors
        image = listing_image
    if not image:
        # Fallback: item-listing--image wrapper or any img.img-fluid (skip logos/SVGs)
        for img_el in soup.select("a.item-listing--image img, .item-listing--image img, img.img-fluid"):
            src = img_el.get("src") or img_el.get("data-src") or ""
            if src and "logo" not in src.lower() and not src.lower().endswith(".svg"):
                image = src
                break

    # --- Venue / Address / City ---
    venue = ""
    address = ""
    city = _city_from_url(url)
    country = _country_from_url(url)

    if jsonld and isinstance(jsonld.get("location"), dict):
        loc = jsonld["location"]
        venue = (loc.get("name") or "").strip()
        addr_obj = loc.get("address", {})
        if isinstance(addr_obj, dict):
            addr_name = addr_obj.get("name", "") or addr_obj.get("streetAddress", "")
            parts = [p.strip() for p in addr_name.split(",")]
            clean_parts = [p for p in parts if p.lower() not in COUNTRY_NAMES]
            if not venue:
                venue = clean_parts[0] if clean_parts else ""
            addr_parts = clean_parts[1:] if len(clean_parts) > 1 else clean_parts
            address = ", ".join(addr_parts) if addr_parts else addr_name
        elif isinstance(addr_obj, str):
            address = addr_obj

    # Override photo for Tanec pri fontáne with custom pool
    if TANEC_PHOTO_URLS and TANEC_TITLE_RE.search(title):
        image = random.choice(TANEC_PHOTO_URLS)

    return {
        "source_url": url,
        "source": "eventland",
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
        "min_price": 0.0 if not price_raw or "€" not in price_raw else 999.0,
        "price_raw": price_raw,
        "is_recurring": is_recurring,
        "recurring_end_date": recurring_end_date or None,
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


def _parse_iso_time(date_str: str) -> str:
    """'2026-05-23T14:00:00' → '14:00', '2026-05-23' → ''"""
    try:
        if "T" in date_str:
            time_part = date_str.split("T")[1][:5]  # "14:00"
            return time_part if ":" in time_part else ""
        return ""
    except Exception:
        return ""


def _parse_date_range(date_str: str) -> tuple[str, str]:
    """'27.05.2026 - 23.09.2026' → ('2026-05-27', '2026-09-23')"""
    parts = date_str.split(" - ", 1)
    start = _parse_sk_date(parts[0].strip())
    end = _parse_sk_date(parts[1].strip()) if len(parts) > 1 else ""
    return start, end


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
    # Matches /{locale}/{city}/event/ or /{city}/event/
    m = re.search(r"/(?:[a-z]{2}/)?([^/]+)/event/", url)
    if m:
        slug = m.group(1)
        if slug in CITY_MAP:
            return CITY_MAP[slug]
        return slug.split("-")[0].capitalize()
    return ""


def _country_from_url(url: str) -> str:
    """Extract country code from Eventland URL slug."""
    m = re.search(r"/(?:[a-z]{2}/)?([^/]+)/event/", url)
    if m:
        return CITY_COUNTRY.get(m.group(1), "SK")
    return "SK"


def _is_free(event: dict) -> bool:
    """Free if price_raw contains no paid amount in any currency."""
    price_raw = (event.get("price_raw") or "").strip()
    if not price_raw:
        return True
    # Reject if there's a non-zero numeric amount followed by any currency
    if re.search(r'\b[1-9]\d*\s*(czk|kč|€|eur|£|gbp|huf|pln)', price_raw, re.I):
        return False
    if "€" in price_raw or "£" in price_raw:
        return False
    return True


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
