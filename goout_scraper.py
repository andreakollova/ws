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
    "https://goout.net/sk/bratislava/akcie/leztaofti/",
    "https://goout.net/sk/kosice/akcie/leztaofti/",
]
BASE_URL = "https://goout.net"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WoevaEventsBot/1.0; +https://woeva.com)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.5",
}
MAX_EVENTS = 20
MAX_CHECKS = 30   # stop after checking this many unknown URLs (prevents timeout)
CRAWL_DELAY = 0.8

# GoOut event URL pattern: /sk/[event-slug]/[sz+alphanum-id]/
SCHEDULE_RE = re.compile(r"^https?://goout\.net/sk/[^/]+/sz[a-z0-9]+/?$", re.I)


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
    checks = 0
    for url in all_event_urls:
        if url in seen_urls:
            continue
        seen_urls.add(url)

        if url in existing_urls:
            logger.debug(f"Skip (known): {url}")
            continue

        if checks >= MAX_CHECKS:
            logger.info(f"GoOut: reached MAX_CHECKS ({MAX_CHECKS}), stopping early")
            break
        checks += 1

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


def _page_says_zadarmo(soup: BeautifulSoup) -> bool:
    """Return True if the GoOut event page explicitly shows free entry.

    Primary: look for the ticket-button element (the price pill) containing 'Zadarmo'.
    This is the most reliable signal — GoOut renders a <button class="ticket-button">
    with the price text; free events show 'Zadarmo' there.
    Fallback: normalize full page text and check for free phrases.
    """
    # Primary: ticket-button price pill
    for btn in soup.find_all("button"):
        classes = btn.get("class") or []
        if "ticket-button" in classes:
            if "zadarmo" in btn.get_text(strip=True).lower():
                return True

    # Fallback: full-page text (handles layout variations)
    raw = soup.get_text(" ", strip=True)
    text = " ".join(raw.split()).lower()
    FREE_PHRASES = [
        "vstupné zadarmo", "vstupne zadarmo",
        "vstup zadarmo", "vstup zdarma",
        "vstupné zdarma", "vstupné voľné",
        "free entry", "free admission",
        "vstupné: zadarmo",
    ]
    return any(phrase in text for phrase in FREE_PHRASES)


def _scrape_event_detail(url: str) -> dict | None:
    r = _fetch(url)
    if r is None:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    page_free = _page_says_zadarmo(soup)

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
                event = _parse_jsonld_event(data, url)
                event["page_free"] = page_free
                return event
        except Exception as e:
            logger.debug(f"JSON-LD parse error for {url}: {e}")

    # Fallback: basic HTML extraction
    event = _parse_html_fallback(soup, url)
    if event:
        event["page_free"] = page_free
    return event


def _parse_jsonld_event(data: dict, url: str) -> dict:
    # Price — handles "0", "0–36", "100–300", None
    # Only FREE if max price is also 0 (not just "0–36" which means paid tiers exist)
    offers = data.get("offers", {})
    if isinstance(offers, list):
        raw_prices = [o.get("lowPrice", o.get("price", "")) for o in offers]
        min_price_val = min(_parse_min_price(p) for p in raw_prices) if raw_prices else 999.0
        max_price_val = max(_parse_max_price(p) for p in raw_prices) if raw_prices else 999.0
    else:
        raw_price = offers.get("lowPrice", offers.get("price", ""))
        min_price_val = _parse_min_price(raw_price)
        max_price_val = _parse_max_price(raw_price)

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

    # Dates — GoOut uses JS format "Fri Jul 10 2026 12:00:00 GMT+0000 (...)"
    start_raw = data.get("startDate", "")
    end_raw = data.get("endDate", "")
    start_date, start_time = _parse_goout_date(start_raw)
    _, end_time_unused = _parse_goout_date(end_raw)

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
        "date": start_date,
        "time_start": start_time,
        "duration": _calc_duration(start_raw, end_raw),
        "venue": venue_name,
        "address": address_str,
        "city": city,
        "photo_url": image,
        "min_price": min_price_val,
        "max_price": max_price_val,
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


def _parse_goout_date(date_str: str) -> tuple[str, str]:
    """Parse GoOut date — handles both ISO and JS format.
    'Fri Jul 10 2026 12:00:00 GMT+0000 (...)' → ('2026-07-10', '12:00')
    '2026-07-10T12:00:00+00:00'               → ('2026-07-10', '12:00')
    """
    if not date_str:
        return "", ""
    # ISO format
    if "T" in date_str:
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        except Exception:
            pass
    # JS date format: "Fri Jul 10 2026 12:00:00 GMT+0000 (Coordinated Universal Time)"
    try:
        clean = re.sub(r"\(.*?\)", "", date_str)  # remove (...) timezone name
        clean = re.sub(r"GMT[+-]\d{4}", "", clean).strip()
        dt = datetime.strptime(clean, "%a %b %d %Y %H:%M:%S")
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except Exception:
        pass
    return "", ""


def _parse_min_price(price_str) -> float:
    """Parse price string → minimum float. '0'→0.0, '0–36'→0.0, '100–300'→100.0"""
    if price_str is None or price_str == "":
        return 999.0
    s = str(price_str).replace("\u2013", "-").replace("\u2014", "-").replace("–", "-").replace("—", "-")
    part = s.split("-")[0].strip()
    try:
        return float(part)
    except (ValueError, TypeError):
        return 999.0


def _parse_max_price(price_str) -> float:
    """Parse price string → maximum float. '0'→0.0, '0–36'→36.0, '100–300'→300.0"""
    if price_str is None or price_str == "":
        return 999.0
    s = str(price_str).replace("\u2013", "-").replace("\u2014", "-").replace("–", "-").replace("—", "-")
    parts = s.split("-")
    try:
        return float(parts[-1].strip())
    except (ValueError, TypeError):
        return 999.0


def _is_free(event: dict) -> bool:
    """Event is free if the page explicitly says 'Zadarmo' / 'Vstupné: Zadarmo'."""
    return event.get("page_free", False)


def _calc_duration(start: str, end: str) -> str:
    if not start or not end:
        return ""
    try:
        s_date, s_time = _parse_goout_date(start)
        e_date, e_time = _parse_goout_date(end)
        if not s_date or not e_date:
            return ""
        s = datetime.strptime(f"{s_date} {s_time or '00:00'}", "%Y-%m-%d %H:%M")
        e = datetime.strptime(f"{e_date} {e_time or '00:00'}", "%Y-%m-%d %H:%M")
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
