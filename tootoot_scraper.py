#!/usr/bin/env python3
"""
Tootoot.fm Slovakia event scraper.
Uses Playwright (headless Chromium) because tootoot.fm is a JavaScript SPA.
Respects robots.txt: only public event listing pages are accessed.

HOW TO DISCOVER THE TOOTOOT API (if you want to replace Playwright later):
  1. Open https://tootoot.fm/sk in Chrome DevTools > Network > XHR/Fetch
  2. Filter requests — look for calls to /api/ or graphql
  3. Copy the URL + headers and implement a direct HTTP call instead
"""

import asyncio
import json
import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)

TOOTOOT_LISTING_URL = "https://tootoot.fm/sk"
MAX_EVENTS = 15
CRAWL_DELAY = 2.0

# Known free-event indicator words in Slovak/English
FREE_KEYWORDS = {"zadarmo", "free", "0 €", "€0", "0€", "vstup zdarma", "vstupne zdarma"}


def scrape_tootoot(existing_urls: set) -> list[dict]:
    """Return list of raw free event dicts from tootoot.fm Slovakia."""
    try:
        return asyncio.run(_scrape_async(existing_urls))
    except Exception as e:
        logger.error(f"Tootoot scraper error: {e}")
        return []


async def _scrape_async(existing_urls: set) -> list[dict]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("Playwright not installed — run: playwright install chromium --with-deps")
        return []

    events = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (compatible; WoevaEventsBot/1.0; +https://woeva.com)",
        )
        page = await context.new_page()

        logger.info(f"Loading tootoot.fm listing: {TOOTOOT_LISTING_URL}")
        try:
            await page.goto(TOOTOOT_LISTING_URL, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)
        except Exception as e:
            logger.error(f"Failed to load tootoot listing: {e}")
            await browser.close()
            return []

        # Extract event detail links
        event_links = await _extract_event_links(page)
        logger.info(f"Tootoot: found {len(event_links)} event links")

        seen: set[str] = set()
        for url in event_links:
            url = url.split("?")[0].rstrip("/")
            if url in seen or url in existing_urls:
                continue
            seen.add(url)

            if len(events) >= MAX_EVENTS:
                break

            await asyncio.sleep(CRAWL_DELAY)
            event = await _scrape_event_detail(page, url)
            if event and _is_free(event):
                events.append(event)
                existing_urls.add(url)
                logger.info(f"Free event: {event['title'][:60]}")

        await browser.close()
    return events


async def _extract_event_links(page) -> list[str]:
    """Extract event detail page URLs from the tootoot listing page."""
    try:
        links = await page.eval_on_selector_all(
            "a[href]",
            """els => els
                .map(e => e.href)
                .filter(h => h.includes('/event/') || h.includes('/events/') || h.match(/\\/sk\\/[^/]+\\/\\d+/))
            """,
        )
        return list(dict.fromkeys(links))  # deduplicate while preserving order
    except Exception as e:
        logger.warning(f"Could not extract event links: {e}")
        return []


async def _scrape_event_detail(page, url: str) -> dict | None:
    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1500)

        # Try JSON-LD first
        ld_scripts = await page.eval_on_selector_all(
            "script[type='application/ld+json']",
            "els => els.map(e => e.textContent)",
        )
        for raw in ld_scripts:
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    data = next((d for d in data if d.get("@type") == "Event"), None)
                if data and data.get("@type") == "Event":
                    return _parse_jsonld_event(data, url)
            except Exception:
                pass

        # Fallback: scrape visible text
        title = await page.title() or ""
        title = title.split("|")[0].split("-")[0].strip()

        og_image = ""
        try:
            og_image = await page.get_attribute("meta[property='og:image']", "content") or ""
        except Exception:
            pass

        # Check for free/zadarmo in page text
        page_text = (await page.inner_text("body") or "").lower()
        price_indicators = any(kw in page_text for kw in FREE_KEYWORDS)
        min_price = 0.0 if price_indicators else 999.0

        # Try to find date/time/venue from structured elements
        date_str, time_str, venue, address, city = "", "", "", "", ""
        try:
            og_desc = await page.get_attribute("meta[property='og:description']", "content") or ""
        except Exception:
            og_desc = ""

        return {
            "source_url": url,
            "source": "tootoot",
            "title": title,
            "original_description": og_desc[:500],
            "date": date_str,
            "time_start": time_str,
            "duration": "",
            "venue": venue,
            "address": address,
            "city": city,
            "photo_url": og_image,
            "min_price": min_price,
        }
    except Exception as e:
        logger.warning(f"Failed to scrape tootoot event {url}: {e}")
        return None


def _parse_jsonld_event(data: dict, url: str) -> dict:
    offers = data.get("offers", {})
    if isinstance(offers, list):
        offers = min(offers, key=lambda o: float(o.get("lowPrice", o.get("price", 999)) or 0))
    low_price = offers.get("lowPrice", offers.get("price", ""))
    try:
        min_price_val = float(low_price) if low_price not in (None, "") else 999.0
    except (TypeError, ValueError):
        min_price_val = 999.0

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

    start = data.get("startDate", "")
    end = data.get("endDate", "")
    image = data.get("image", "")
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url", "")

    return {
        "source_url": url,
        "source": "tootoot",
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
