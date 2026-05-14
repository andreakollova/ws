#!/usr/bin/env python3
"""
KudyZNudy.cz Prague free events scraper.
Uses the public JSON API endpoint that backs the filtered listing page.
Filters: city=Praha, free admission (zdarma).
"""

import logging
import re
import time
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://www.kudyznudy.cz"
# JSON API used by the listing page — returns paginated event results
API_URL = "https://www.kudyznudy.cz/api/event/list"
CRAWL_DELAY = 1.5
MAX_PAGES = 10
PAGE_SIZE = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WoevaEventsBot/1.0; +https://woeva.com)",
    "Accept": "application/json, text/html;q=0.9",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.5",
    "Referer": "https://www.kudyznudy.cz/kalendar-akci/",
    "X-Requested-With": "XMLHttpRequest",
}

# API query params for Praha free events
BASE_PARAMS = {
    "region": "Praha",
    "admission": "free",
    "pageSize": PAGE_SIZE,
}

FREE_KEYWORDS = ["zdarma", "vstup zdarma", "vstup volný", "free", "vstupné zdarma"]


def scrape_kudyznudy(existing_urls: set) -> list[dict]:
    """Return list of new free Prague events from kudyznudy.cz not in DB."""
    all_events: list[dict] = []

    for page in range(1, MAX_PAGES + 1):
        params = {**BASE_PARAMS, "page": page}
        logger.info(f"KudyZNudy: fetching page {page}")
        data = _fetch_json(API_URL, params)

        if data is None:
            # API not available — fall back to HTML scraping
            logger.info("KudyZNudy API unavailable, trying HTML fallback")
            all_events.extend(_scrape_html(existing_urls))
            break

        items = data.get("items") or data.get("events") or data.get("data") or []
        if not items and isinstance(data, list):
            items = data

        if not items:
            logger.info(f"KudyZNudy: no items on page {page}, stopping")
            break

        for item in items:
            event = _parse_api_item(item)
            if event is None:
                continue
            if event["source_url"] in existing_urls:
                continue
            all_events.append(event)
            existing_urls.add(event["source_url"])

        logger.info(f"  Page {page}: {len(items)} items")
        if len(items) < PAGE_SIZE:
            break
        time.sleep(CRAWL_DELAY)

    logger.info(f"KudyZNudy total new events: {len(all_events)}")
    return all_events


def _fetch_json(url: str, params: dict | None = None) -> dict | list | None:
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=20)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"KudyZNudy API fetch failed: {e}")
        return None


def _fetch_html(url: str) -> requests.Response | None:
    try:
        r = requests.get(url, headers={**HEADERS, "Accept": "text/html"}, timeout=20)
        r.raise_for_status()
        return r
    except Exception as e:
        logger.warning(f"KudyZNudy HTML fetch failed {url}: {e}")
        return None


def _parse_api_item(item: dict) -> dict | None:
    """Parse a single event item from the JSON API response."""
    title = (item.get("name") or item.get("title") or "").strip()
    if not title:
        return None

    slug = item.get("url") or item.get("slug") or item.get("permalink") or ""
    if slug and not slug.startswith("http"):
        slug = BASE_URL + slug
    source_url = slug or ""
    if not source_url:
        return None

    # Date
    date_raw = item.get("dateFrom") or item.get("startDate") or item.get("date") or ""
    date = _parse_date(date_raw)

    time_start = ""
    if date_raw and "T" in date_raw:
        time_start = date_raw.split("T")[1][:5]

    description = (
        item.get("perex") or item.get("description") or item.get("shortDescription") or ""
    ).strip()[:1000]

    image_url = item.get("image") or item.get("imageUrl") or item.get("photo") or None
    if image_url and not image_url.startswith("http"):
        image_url = BASE_URL + image_url

    venue = (item.get("place") or item.get("venue") or item.get("location") or "").strip()
    address = (item.get("address") or "").strip()

    return {
        "source_url": source_url,
        "source": "kudyznudy",
        "title": title,
        "original_description": description,
        "date": date,
        "time_start": time_start,
        "duration": "",
        "venue": venue,
        "address": address,
        "city": "Prague",
        "country": "CZ",
        "photo_url": image_url,
        "min_price": 0.0,
        "price_raw": "Zdarma",
        "is_free": True,
        "price": 0.0,
    }


def _scrape_html(existing_urls: set) -> list[dict]:
    """
    HTML fallback: scrape the filtered listing page directly.
    kudyznudy.cz uses JS rendering, so this may yield limited results
    from the initial server-rendered HTML only.
    """
    from bs4 import BeautifulSoup

    listing_url = (
        "https://www.kudyznudy.cz/kalendar-akci/"
        "?filtr=zdarma&kraj=Praha&mesto=Praha"
    )
    r = _fetch_html(listing_url)
    if r is None:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    events: list[dict] = []

    # Try to find embedded JSON in __NEXT_DATA__ or window.__STATE__
    json_data = _extract_embedded_json(soup)
    if json_data:
        items = (
            _deep_get_list(json_data, "props", "pageProps", "events")
            or _deep_get_list(json_data, "props", "pageProps", "items")
            or []
        )
        for item in items:
            event = _parse_api_item(item)
            if event and event["source_url"] not in existing_urls:
                events.append(event)
                existing_urls.add(event["source_url"])
        if events:
            return events

    # Last resort: parse visible event cards from HTML
    cards = soup.find_all("article") or soup.find_all("div", class_=re.compile(r"event|card|item", re.I))
    for card in cards:
        title_el = card.find(["h2", "h3", "h4"])
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        link_el = card.find("a", href=True)
        if not link_el:
            continue
        href = link_el["href"]
        if not href.startswith("http"):
            href = BASE_URL + href
        if href in existing_urls:
            continue

        text = card.get_text(" ", strip=True).lower()
        if not any(k in text for k in FREE_KEYWORDS):
            continue

        img_el = card.find("img")
        image_url = img_el.get("src") or img_el.get("data-src") if img_el else None

        events.append({
            "source_url": href,
            "source": "kudyznudy",
            "title": title,
            "original_description": "",
            "date": "",
            "time_start": "",
            "duration": "",
            "venue": "",
            "address": "",
            "city": "Prague",
            "country": "CZ",
            "photo_url": image_url,
            "min_price": 0.0,
            "price_raw": "Zdarma",
            "is_free": True,
            "price": 0.0,
        })
        existing_urls.add(href)

    return events


def _extract_embedded_json(soup) -> dict | None:
    import json
    for script in soup.find_all("script"):
        src = script.string or ""
        if "__NEXT_DATA__" in (script.get("id") or ""):
            try:
                return json.loads(src)
            except Exception:
                pass
        for prefix in ("window.__STATE__=", "window.__INITIAL_STATE__=", "__NUXT__="):
            if src.strip().startswith(prefix):
                try:
                    return json.loads(src[len(prefix):].rstrip(";"))
                except Exception:
                    pass
    return None


def _deep_get_list(obj: dict, *keys) -> list:
    for key in keys:
        if not isinstance(obj, dict):
            return []
        obj = obj.get(key, {})
    return obj if isinstance(obj, list) else []


def _parse_date(date_str: str) -> str:
    if not date_str:
        return ""
    try:
        return date_str[:10]  # ISO format "YYYY-MM-DD..."
    except Exception:
        return ""
