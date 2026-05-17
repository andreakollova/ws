#!/usr/bin/env python3
"""
FreshMarket Bratislava scraper — farmárska tržnica.
Scrapes upcoming events from freshmarket.sk/podujatia.
"""

import logging
import re
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://www.freshmarket.sk"
LISTING_URL = "https://www.freshmarket.sk/podujatia"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept-Language": "sk-SK,sk;q=0.9",
}
MAX_EVENTS = 20


def _parse_sk_date(text: str) -> str | None:
    """Convert '30.5.2026' or '30. 5. 2026' to '2026-05-30'."""
    m = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _scrape_event_detail(url: str) -> dict | None:
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        res.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"FreshMarket: detail fetch failed {url}: {e}")
        return None

    soup = BeautifulSoup(res.text, "html.parser")

    # Title
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""
    if not title:
        return None

    # infoBox: "30.5.2026 | | | Sobota | 10:00"
    date_str = None
    time_str = None
    info_box = soup.find(class_="infoBox")
    if info_box:
        text = info_box.get_text(separator=" ", strip=True)
        date_str = _parse_sk_date(text)
        time_match = re.search(r"\b(\d{1,2}:\d{2})\b", text)
        if time_match:
            time_str = time_match.group(1)

    # Description — first substantial paragraph
    description = ""
    for p in soup.find_all("p"):
        t = p.get_text(strip=True)
        if len(t) > 60:
            description = t[:400]
            break

    # Cover image — prefer /static/ images, not SVG icons
    photo_url = None
    og = soup.find("meta", property="og:image")
    if og and og.get("content") and not og["content"].endswith(".svg"):
        photo_url = og["content"]
    if not photo_url:
        for img in soup.find_all("img", src=True):
            src = img["src"]
            if src.startswith("data:") or src.endswith(".svg"):
                continue
            if any(ext in src for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                photo_url = BASE_URL + src if src.startswith("/") else src
                break

    return {
        "title": title,
        "description": description,
        "date": date_str,
        "time_start": time_str or "10:00",
        "duration": "9h",
        "venue": "FreshMarket",
        "address": "Trnavské mýto, Bratislava",
        "city": "Bratislava",
        "country": "SK",
        "tag": "trhy",
        "photo_url": photo_url or "",
        "source_url": url,
        "source": "freshmarket",
        "is_recurring": False,
    }


def scrape_freshmarket(existing_urls: set) -> list[dict]:
    logger.info(f"Fetching FreshMarket listing: {LISTING_URL}")
    try:
        res = requests.get(LISTING_URL, headers=HEADERS, timeout=20)
        res.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"FreshMarket listing fetch failed: {e}")
        return []

    soup = BeautifulSoup(res.text, "html.parser")

    # Collect event slugs from links like /podujatia/some-event/
    slug_re = re.compile(r"^/podujatia/([^/]+)/?$")
    seen_urls: set[str] = set()
    event_urls: list[str] = []

    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0].rstrip("/") + "/"
        if slug_re.match(href.rstrip("/")):
            full = BASE_URL + href.rstrip("/") + "/"
            if full not in existing_urls and full not in seen_urls:
                seen_urls.add(full)
                event_urls.append(full)

    logger.info(f"FreshMarket: {len(event_urls)} new event URLs found")

    today = datetime.now(timezone.utc).date()
    events: list[dict] = []

    for url in event_urls[:MAX_EVENTS]:
        event = _scrape_event_detail(url)
        if not event:
            continue
        # Skip events without a date or past events
        if event.get("date"):
            try:
                edate = datetime.strptime(event["date"], "%Y-%m-%d").date()
                if edate < today:
                    continue
            except ValueError:
                pass
        events.append(event)
        logger.info(f"FreshMarket: scraped '{event['title']}' ({event.get('date')})")
        time.sleep(0.5)

    logger.info(f"FreshMarket: {len(events)} upcoming events")
    return events
