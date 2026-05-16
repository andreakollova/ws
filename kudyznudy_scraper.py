#!/usr/bin/env python3
"""
Kudyznudy.cz Prague free events scraper — uses Playwright for JS-rendered content.
Scrapes the free events calendar for Praha region.
"""

import logging
import re
import time
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

LISTING_URLS = [
    "https://www.kudyznudy.cz/kalendar-akci/hlavni-mesto-praha?filtr=zdarma",
    "https://www.kudyznudy.cz/kalendar-akci/hudebni-a-divadelni-akce/hlavni-mesto-praha?filtr=zdarma",
]
BASE_URL = "https://www.kudyznudy.cz"
MAX_EVENTS = 40
CRAWL_DELAY = 1.5

# Match event detail URLs like /akce/nazov-eventu or /tip/nazov
EVENT_URL_RE = re.compile(r"^https?://www\.kudyznudy\.cz/(akce|tip)/[^/]+", re.I)

# Czech month names → month number
CZ_MONTHS = {
    "ledna": 1, "února": 2, "března": 3, "dubna": 4,
    "května": 5, "června": 6, "července": 7, "srpna": 8,
    "září": 9, "října": 10, "listopadu": 11, "prosince": 12,
}


def _parse_czech_date(text: str):
    """Parse Czech date strings like '17. května 2026' or '17. 5. 2026'."""
    text = text.strip()
    # Format: 17. května 2026
    m = re.search(r"(\d{1,2})\.\s+(\w+)\s+(\d{4})", text)
    if m:
        day, month_str, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        month = CZ_MONTHS.get(month_str)
        if month:
            try:
                return datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                pass
    # Format: 17. 5. 2026
    m = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", text)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _parse_time(text: str):
    """Extract HH:MM from text."""
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def _scrape_detail(page, url: str) -> dict:
    """Fetch a single event detail page and extract structured data."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        time.sleep(0.5)
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        title_el = soup.find("h1") or soup.find(class_=re.compile(r"title|heading|nadpis", re.I))
        title = title_el.get_text(strip=True) if title_el else ""

        # Description
        desc_el = soup.find(class_=re.compile(r"perex|description|intro|text", re.I))
        description = desc_el.get_text(strip=True)[:500] if desc_el else ""

        # Image
        photo_url = ""
        og_img = soup.find("meta", property="og:image")
        if og_img:
            photo_url = og_img.get("content", "")
        if not photo_url:
            img = soup.find("img", class_=re.compile(r"main|hero|cover|detail", re.I))
            if img:
                photo_url = img.get("src", "")
        if photo_url and not photo_url.startswith("http"):
            photo_url = urljoin(BASE_URL, photo_url)

        # Date / time — look in the whole page text
        page_text = soup.get_text(" ", strip=True)
        date = _parse_czech_date(page_text)
        time_start = _parse_time(page_text)

        # Venue / address
        venue = ""
        address = ""
        venue_el = soup.find(class_=re.compile(r"venue|misto|place|address|adresa", re.I))
        if venue_el:
            venue = venue_el.get_text(strip=True)[:200]

        return {
            "title": title,
            "description": description,
            "photo_url": photo_url,
            "date": date,
            "time_start": time_start,
            "venue": venue,
            "address": address,
            "city": "Prague",
            "country": "CZ",
            "source_url": url,
            "source": "kudyznudy",
        }
    except Exception as e:
        logger.warning(f"Detail scrape failed for {url}: {e}")
        return {}


def scrape_kudyznudy(existing_urls: set) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("playwright not installed — run: pip install playwright && playwright install chromium")
        return []

    events = []
    seen_urls: set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="cs-CZ",
        )
        page = context.new_page()

        for listing_url in LISTING_URLS:
            if len(events) >= MAX_EVENTS:
                break
            try:
                logger.info(f"Kudyznudy: loading {listing_url}")
                page.goto(listing_url, wait_until="networkidle", timeout=30000)
                time.sleep(2)

                html = page.content()
                soup = BeautifulSoup(html, "html.parser")

                # Collect all event detail links
                event_links: list[str] = []
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if not href.startswith("http"):
                        href = urljoin(BASE_URL, href)
                    if EVENT_URL_RE.match(href) and href not in seen_urls and href not in existing_urls:
                        event_links.append(href)
                        seen_urls.add(href)

                logger.info(f"Kudyznudy: found {len(event_links)} new event links on {listing_url}")

                for event_url in event_links[:MAX_EVENTS]:
                    if len(events) >= MAX_EVENTS:
                        break
                    data = _scrape_detail(page, event_url)
                    if data.get("title") and data.get("date"):
                        events.append(data)
                        logger.info(f"Kudyznudy: scraped '{data['title'][:60]}'")
                    time.sleep(CRAWL_DELAY)

            except Exception as e:
                logger.error(f"Kudyznudy listing failed for {listing_url}: {e}", exc_info=True)

        browser.close()

    logger.info(f"Kudyznudy: total {len(events)} new events")
    return events
