#!/usr/bin/env python3
"""
CityBee.cz Prague events scraper.
Scrapes /lifestyle/ and /kultura/ sections using iCal export endpoints.
Respects robots.txt (verified: only /admin/, /config/ etc. are disallowed).
"""

import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://www.citybee.cz"
LISTING_URLS = [
    "https://www.citybee.cz/lifestyle/akce/prehled/",
    "https://www.citybee.cz/kultura/akce/prehled/",
]
HEADERS = {
    "User-Agent": "WoevaBot/1.0 (+https://woeva.app)",
    "Accept-Language": "cs-CZ,cs;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}
MAX_EVENTS = 40
CRAWL_DELAY = 1.5  # be polite

# Match event URLs like /akce/12345-slug/
EVENT_URL_RE = re.compile(r"^https://www\.citybee\.cz/akce/\d+-[^/]+/?$", re.I)

_robots: RobotFileParser | None = None

def _can_fetch(url: str) -> bool:
    global _robots
    if _robots is None:
        _robots = RobotFileParser()
        _robots.set_url(f"{BASE_URL}/robots.txt")
        try:
            _robots.read()
        except Exception:
            return True  # if robots.txt unreachable, allow
    return _robots.can_fetch(HEADERS["User-Agent"], url)


def _parse_ical_date(dtstring: str):
    """Parse iCal DTSTART like 20260519T180000Z → ('2026-05-19', '20:00') CET."""
    try:
        dt = datetime.strptime(dtstring[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        # Convert UTC to CET (UTC+2 in summer)
        from datetime import timedelta
        dt_local = dt + timedelta(hours=2)
        return dt_local.strftime("%Y-%m-%d"), dt_local.strftime("%H:%M")
    except Exception:
        return None, None


def _fetch_ical(session: requests.Session, event_url: str) -> dict:
    """Fetch /export/ iCal for an event and return parsed fields."""
    export_url = event_url.rstrip("/") + "/export/"
    try:
        res = session.get(export_url, headers=HEADERS, timeout=10)
        if res.status_code != 200:
            return {}
        text = res.text

        def _ical_field(name: str) -> str:
            m = re.search(rf"^{name}[^:]*:(.+)$", text, re.MULTILINE)
            return m.group(1).strip() if m else ""

        summary = _ical_field("SUMMARY;LANGUAGE=cs") or _ical_field("SUMMARY")
        description = _ical_field("DESCRIPTION;LANGUAGE=cs") or _ical_field("DESCRIPTION")
        location = _ical_field("LOCATION;LANGUAGE=cs") or _ical_field("LOCATION")
        dtstart = _ical_field("DTSTART")
        dtend = _ical_field("DTEND")
        # Extract venue name from ORGANIZER CN field: ORGANIZER;CN="Kavárna co hledá jméno":MAILTO:...
        organizer_m = re.search(r'ORGANIZER[^:]*CN="([^"]+)"', text)
        organizer_name = organizer_m.group(1).strip() if organizer_m else ""

        date, time_start = _parse_ical_date(dtstart)
        _, time_end = _parse_ical_date(dtend)

        duration = None
        if time_start and time_end:
            try:
                h1, m1 = map(int, time_start.split(":"))
                h2, m2 = map(int, time_end.split(":"))
                duration = round((h2 * 60 + m2 - h1 * 60 - m1) / 60, 1)
                if duration <= 0:
                    duration = None
            except Exception:
                pass

        # Venue = organizer name (e.g. "Kavárna co hledá jméno"), address = full LOCATION
        venue = organizer_name
        address = location  # full address like "Stroupežnického 493/10, Praha 5"

        return {
            "title": summary,
            "description": description[:400] if description else "",
            "venue": venue,
            "address": address,
            "date": date,
            "time_start": time_start,
            "duration": duration,
        }
    except Exception as e:
        logger.warning(f"iCal fetch failed for {export_url}: {e}")
        return {}


def _is_future(date_str: str) -> bool:
    if not date_str:
        return False
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date() >= datetime.now().date()
    except Exception:
        return False


def scrape_citybee(existing_urls: set) -> list[dict]:
    session = requests.Session()
    events = []
    seen_urls: set[str] = set()

    for listing_url in LISTING_URLS:
        if not _can_fetch(listing_url):
            logger.warning(f"CityBee: robots.txt disallows {listing_url}, skipping")
            continue

        try:
            res = session.get(listing_url, headers=HEADERS, timeout=15)
            if res.status_code != 200:
                logger.warning(f"CityBee: {listing_url} returned {res.status_code}")
                continue

            soup = BeautifulSoup(res.text, "html.parser")

            # Collect event detail URLs
            event_urls = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if not href.startswith("http"):
                    href = urljoin(BASE_URL, href)
                href = re.sub(r"/export/?$", "/", href)
                if not _can_fetch(href):
                    continue
                if EVENT_URL_RE.match(href) and href not in seen_urls and href not in existing_urls:
                    event_urls.append(href)
                    seen_urls.add(href)

            logger.info(f"CityBee: found {len(event_urls)} event links on {listing_url}")

            for event_url in event_urls:
                if len(events) >= MAX_EVENTS:
                    break

                data = _fetch_ical(session, event_url)
                if not data.get("title") or not data.get("date"):
                    time.sleep(CRAWL_DELAY)
                    continue

                if not _is_future(data["date"]):
                    time.sleep(CRAWL_DELAY * 0.5)
                    continue

                # Get photo and original description from detail page
                photo_url = ""
                original_description = data.get("description", "")
                try:
                    detail_res = session.get(event_url, headers=HEADERS, timeout=10)
                    if detail_res.status_code == 200:
                        detail_soup = BeautifulSoup(detail_res.text, "html.parser")
                        og = detail_soup.find("meta", property="og:image")
                        if og:
                            photo_url = og.get("content", "")
                        og_desc = detail_soup.find("meta", property="og:description")
                        if og_desc and og_desc.get("content"):
                            original_description = og_desc["content"][:500]
                except Exception:
                    pass

                events.append({
                    "title": data["title"],
                    "original_description": original_description,
                    "description": original_description,
                    "date": data["date"],
                    "time_start": data["time_start"] or "",
                    "duration": data["duration"] or 2.0,
                    "venue": data["venue"],
                    "address": data["address"],
                    "photo_url": photo_url,
                    "city": "Prague",
                    "country": "CZ",
                    "source_url": event_url,
                    "source": "citybee",
                })
                logger.info(f"CityBee: scraped '{data['title'][:60]}'")
                time.sleep(CRAWL_DELAY)

        except Exception as e:
            logger.error(f"CityBee listing failed for {listing_url}: {e}", exc_info=True)

    logger.info(f"CityBee: total {len(events)} new events")
    return events
