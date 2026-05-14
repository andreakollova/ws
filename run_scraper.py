#!/usr/bin/env python3
"""
Woeva events scraper — main cron entry point.
Runs 4x/day via Render cron job.
Scrapes GoOut + Tootoot, filters FREE events, enriches with ChatGPT,
saves new events to Supabase scraped_events table.
"""

import logging
import os
import sys
from pathlib import Path

# Load .env for local development
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from datetime import datetime

from supabase import create_client

from goout_scraper import scrape_goout
from tootoot_scraper import scrape_tootoot
from eventland_scraper import scrape_eventland
from petrzalka_scraper import scrape_petrzalka
from visitkosice_scraper import scrape_visitkosice
from eventbrite_scraper import scrape_eventbrite
from kudyznudy_scraper import scrape_kudyznudy
from heylo_scraper import scrape_heylo
from enrich import enrich_event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def get_supabase():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        logger.error("Missing SUPABASE_URL / SUPABASE_KEY")
        sys.exit(1)
    return create_client(url, key)


def load_existing_urls(db) -> set:
    res = db.table("scraped_events").select("source_url").execute()
    return {row["source_url"] for row in (res.data or [])}


def main():
    logger.info(f"=== Woeva scraper started at {datetime.now():%Y-%m-%d %H:%M:%S} ===")

    db = get_supabase()
    existing_urls = load_existing_urls(db)
    logger.info(f"Known events in DB: {len(existing_urls)}")

    raw_events: list[dict] = []

    # GoOut
    try:
        goout_events = scrape_goout(existing_urls)
        logger.info(f"GoOut: {len(goout_events)} new free events")
        raw_events.extend(goout_events)
    except Exception as e:
        logger.error(f"GoOut scraper failed: {e}", exc_info=True)

    # Tootoot
    try:
        tootoot_events = scrape_tootoot(existing_urls)
        logger.info(f"Tootoot: {len(tootoot_events)} new free events")
        raw_events.extend(tootoot_events)
    except Exception as e:
        logger.error(f"Tootoot scraper failed: {e}", exc_info=True)

    # Eventland
    try:
        eventland_events = scrape_eventland(existing_urls)
        logger.info(f"Eventland: {len(eventland_events)} new free events")
        raw_events.extend(eventland_events)
    except Exception as e:
        logger.error(f"Eventland scraper failed: {e}", exc_info=True)

    # FC Petržalka ženy — upcoming matches
    try:
        petrzalka_events = scrape_petrzalka(existing_urls)
        logger.info(f"Petržalka: {len(petrzalka_events)} new match events")
        raw_events.extend(petrzalka_events)
    except Exception as e:
        logger.error(f"Petržalka scraper failed: {e}", exc_info=True)

    # VisitKošice — free events
    try:
        kosice_events = scrape_visitkosice(existing_urls)
        logger.info(f"VisitKošice: {len(kosice_events)} new free events")
        raw_events.extend(kosice_events)
    except Exception as e:
        logger.error(f"VisitKošice scraper failed: {e}", exc_info=True)

    # Eventbrite London — free events
    try:
        eventbrite_events = scrape_eventbrite(existing_urls)
        logger.info(f"Eventbrite London: {len(eventbrite_events)} new free events")
        raw_events.extend(eventbrite_events)
    except Exception as e:
        logger.error(f"Eventbrite scraper failed: {e}", exc_info=True)

    # KudyZNudy Prague — free events
    try:
        kudyznudy_events = scrape_kudyznudy(existing_urls)
        logger.info(f"KudyZNudy Prague: {len(kudyznudy_events)} new free events")
        raw_events.extend(kudyznudy_events)
    except Exception as e:
        logger.error(f"KudyZNudy scraper failed: {e}", exc_info=True)

    # Heylo groups (Piatkovica Bratislava, ...)
    try:
        heylo_events = scrape_heylo(existing_urls)
        logger.info(f"Heylo: {len(heylo_events)} new events")
        raw_events.extend(heylo_events)
    except Exception as e:
        logger.error(f"Heylo scraper failed: {e}", exc_info=True)

    saved = 0
    for event in raw_events:
        try:
            enriched = enrich_event(event)
            db.table("scraped_events").insert(enriched).execute()
            logger.info(f"Saved: {enriched['title'][:70]}")
            saved += 1
        except Exception as e:
            if "duplicate" in str(e).lower() or "23505" in str(e):
                existing_urls.add(event["source_url"])
                logger.debug(f"Duplicate skipped: {event.get('source_url', '')}")
                continue
            logger.error(f"Save failed for '{event.get('title', '')}': {e}")

    logger.info(f"=== Done. Saved {saved} new events. ===")


if __name__ == "__main__":
    main()
