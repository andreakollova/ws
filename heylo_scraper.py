#!/usr/bin/env python3
"""
Heylo.com group event scraper.
Fetches a Heylo group page and extracts upcoming events from __NEXT_DATA__.
Only free / no-cost events are included (or all, if no price is specified).
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

CRAWL_DELAY = 0.8

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.5",
}

# List of Heylo groups to scrape: (group_id, city, is_free)
HEYLO_GROUPS = [
    {
        "group_id": "963e8315-4791-400a-ab26-1818827f5eeb",
        "city": "Bratislava",
        "country": "SK",
        "is_free": True,   # Piatkovica events are free/community
    },
]


def scrape_heylo(existing_urls: set) -> list[dict]:
    """Return list of new events from all configured Heylo groups."""
    all_events: list[dict] = []

    for group in HEYLO_GROUPS:
        try:
            events = _scrape_group(group, existing_urls)
            logger.info(f"Heylo {group['group_id'][:8]}: {len(events)} new events")
            all_events.extend(events)
        except Exception as e:
            logger.error(f"Heylo scraper failed for group {group['group_id']}: {e}", exc_info=True)
        time.sleep(CRAWL_DELAY)

    return all_events


def _scrape_group(group: dict, existing_urls: set) -> list[dict]:
    group_id = group["group_id"]
    url = f"https://www.heylo.com/g/{group_id}"

    r = _fetch(url)
    if r is None:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    next_data = _extract_next_data(soup)
    if not next_data:
        logger.warning(f"Heylo: no __NEXT_DATA__ found for group {group_id}")
        return []

    raw_events = _find_events_in_next_data(next_data)
    if not raw_events:
        logger.warning(f"Heylo: no events found in __NEXT_DATA__ for group {group_id}")
        return []

    events: list[dict] = []
    for raw in raw_events:
        event = _parse_event(raw, group)
        if event is None:
            continue
        if event["source_url"] in existing_urls:
            continue
        if _is_past(event["date"]):
            continue
        if group["is_free"] and not _looks_free(raw):
            logger.debug(f"Heylo: skipping paid event: {event['title'][:40]}")
            continue
        events.append(event)
        existing_urls.add(event["source_url"])

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


def _extract_next_data(soup: BeautifulSoup) -> dict | None:
    script = soup.find("script", id="__NEXT_DATA__")
    if script and script.string:
        try:
            return json.loads(script.string)
        except Exception as e:
            logger.debug(f"__NEXT_DATA__ parse error: {e}")
    return None


def _find_events_in_next_data(data: dict) -> list[dict]:
    """
    Walk the dehydrated React Query state to find event objects.
    Heylo stores events in dehydratedState.queries[*].state.data
    """
    events: list[dict] = []

    # Path: props.pageProps.dehydratedState.queries
    queries = (
        data.get("props", {})
            .get("pageProps", {})
            .get("dehydratedState", {})
            .get("queries", [])
    )

    for query in queries:
        state_data = query.get("state", {}).get("data", {})
        # Events may be a list directly or nested under a key
        found = _extract_events_from_obj(state_data)
        events.extend(found)

    # Deduplicate by event ID
    seen: set[str] = set()
    unique: list[dict] = []
    for e in events:
        eid = e.get("id") or e.get("eventId") or str(e.get("startTime", ""))
        if eid and eid not in seen:
            seen.add(eid)
            unique.append(e)
    return unique


def _extract_events_from_obj(obj) -> list[dict]:
    """Recursively search for objects that look like Heylo events."""
    results: list[dict] = []
    if isinstance(obj, list):
        for item in obj:
            results.extend(_extract_events_from_obj(item))
    elif isinstance(obj, dict):
        # A Heylo event typically has startTime (unix ms) and a name/title
        if _looks_like_event(obj):
            results.append(obj)
        else:
            for v in obj.values():
                results.extend(_extract_events_from_obj(v))
    return results


def _looks_like_event(obj: dict) -> bool:
    """Heuristic: dict has a time field (int) and name or title."""
    has_time = isinstance(obj.get("startTime") or obj.get("timestamp") or obj.get("startDate"), (int, float))
    has_name = bool(obj.get("name") or obj.get("title") or obj.get("eventName"))
    return has_time and has_name


def _looks_free(raw: dict) -> bool:
    """Return True if event appears free (no price field or price=0)."""
    price = raw.get("price") or raw.get("cost") or raw.get("ticketPrice") or 0
    if isinstance(price, str):
        price = price.strip()
        return price in ("", "0", "Free", "free", "Zdarma", "zdarma")
    return float(price) == 0


def _parse_event(raw: dict, group: dict) -> dict | None:
    # Title
    title = (raw.get("name") or raw.get("title") or raw.get("eventName") or "").strip()
    if not title:
        return None
    # Strip city prefix like [BRATISLAVA], [KOSICE], etc.
    import re as _re
    title = _re.sub(r'^\[[^\]]+\]\s*', '', title).strip()

    # Event ID and source URL
    event_id = raw.get("id") or raw.get("eventId") or ""
    deep_link = raw.get("deepLink") or ""
    if deep_link:
        source_url = f"https://www.heylo.com{deep_link}" if deep_link.startswith("/") else deep_link
    elif event_id:
        source_url = f"https://www.heylo.com/event/{event_id}?redirect=0&context=group-page"
    else:
        source_url = f"https://www.heylo.com/g/{group['group_id']}"

    # Date + time — Heylo uses "timestamp" field (Unix ms)
    start_ts = raw.get("startTime") or raw.get("timestamp") or raw.get("startDate") or 0
    date_str = ""
    time_str = ""
    if start_ts:
        if start_ts > 1e10:
            start_ts = start_ts / 1000
        try:
            dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
            date_str = dt.strftime("%Y-%m-%d")
            time_str = dt.strftime("%H:%M")
        except Exception:
            pass

    # Description from group page data
    description = (
        raw.get("description") or raw.get("body") or raw.get("about") or ""
    ).strip()[:1000]

    # Image — try multiple field names
    image_url = (
        raw.get("imageUrl") or raw.get("coverUrl") or raw.get("coverImageUrl") or
        raw.get("photo") or raw.get("image") or raw.get("coverPhoto") or
        raw.get("thumbnailUrl") or None
    )

    # Venue — may be string or dict
    venue_raw = (
        raw.get("locationName") or raw.get("location") or
        raw.get("venue") or raw.get("address") or ""
    )
    if isinstance(venue_raw, dict):
        venue_raw = venue_raw.get("name") or venue_raw.get("address") or ""
    venue = str(venue_raw).strip()

    # If image or description missing, fetch the event detail page
    if (not image_url or not description) and event_id:
        detail = _fetch_event_detail(event_id)
        if detail:
            if not image_url:
                image_url = detail.get("image_url")
            if not description:
                description = detail.get("description", "")
            if not venue:
                venue = detail.get("venue", "")

    return {
        "source_url": source_url,
        "source": "heylo",
        "title": title,
        "original_description": description,
        "date": date_str,
        "time_start": time_str,
        "duration": "",
        "venue": venue,
        "address": "",
        "city": group["city"],
        "country": group["country"],
        "photo_url": image_url,
        "min_price": 0.0,
        "price_raw": "Free",
        "is_free": True,
        "price": 0.0,
    }


def _fetch_event_detail(event_id: str) -> dict | None:
    """Fetch individual Heylo event page and extract image/description/venue."""
    url = f"https://www.heylo.com/event/{event_id}?redirect=0&context=group-page"
    time.sleep(CRAWL_DELAY)
    r = _fetch(url)
    if r is None:
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    next_data = _extract_next_data(soup)
    if next_data:
        # Event detail is at queries[*].state.data.event
        queries = (
            next_data.get("props", {})
            .get("pageProps", {})
            .get("dehydratedState", {})
            .get("queries", [])
        )
        for q in queries:
            ev = q.get("state", {}).get("data", {}).get("event")
            if isinstance(ev, dict) and (ev.get("eventId") == event_id or ev.get("id") == event_id):
                image_url = ev.get("image") or ev.get("imageUrl") or None
                description = (ev.get("notes") or ev.get("description") or ev.get("body") or "").strip()[:1000]
                loc_data = ev.get("locationData") or {}
                venue = (loc_data.get("label") or ev.get("location") or "").strip()
                return {"image_url": image_url, "description": description, "venue": venue}

    # Fallback: og tags
    og_image = soup.find("meta", property="og:image")
    og_desc = soup.find("meta", property="og:description")
    return {
        "image_url": og_image.get("content", "").strip() if og_image else None,
        "description": og_desc.get("content", "").strip()[:1000] if og_desc else "",
        "venue": "",
    }


def _find_event_by_id(data: dict, event_id: str) -> dict | None:
    """Walk __NEXT_DATA__ to find the event dict matching event_id."""
    def _walk(obj):
        if isinstance(obj, dict):
            if obj.get("id") == event_id or obj.get("eventId") == event_id:
                if _looks_like_event(obj):
                    return obj
            for v in obj.values():
                result = _walk(v)
                if result:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = _walk(item)
                if result:
                    return result
        return None
    return _walk(data)


def _is_past(date_str: str) -> bool:
    if not date_str:
        return False
    try:
        event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = datetime.now().date()
        return (today - event_date).days > 2
    except Exception:
        return False
