#!/usr/bin/env python3
"""
ChatGPT enrichment for scraped events.
Generates a short Slovak description (1 emoji + max 29 words)
and assigns one category tag from the Woeva predefined list.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone

from openai import OpenAI

logger = logging.getLogger(__name__)

VALID_TAGS = [
    "coffee", "party", "zapasy", "sport", "umenie",
    "gaming", "conference", "priroda", "historia", "zaujimave",
]

SYSTEM_PROMPT = """\
You are an assistant for Woeva, a Slovak community events app. Your job is to enrich event data.

Given an event, you must return a JSON object with two fields:

1. "description": A SHORT, fun, inviting description in SLOVAK language.
   - Start with EXACTLY ONE relevant emoji
   - After the emoji: max 29 words (the emoji does not count toward the word limit)
   - Tone: casual, exciting, welcoming — make people want to go
   - If no original description is available, invent something fitting based on the title/venue

2. "tag": Assign the BEST matching tag from this EXACT list only:
   - coffee    = coffee meetups, casual social gatherings, brunch
   - party     = DJ nights, club events, dancing, nightlife, celebrations
   - zapasy    = sports competitions, matches, tournaments (spectator)
   - sport     = active sports, fitness, yoga, running, cycling (participating)
   - umenie    = art exhibitions, theatre, concerts, film, cultural events
   - gaming    = video games, board games, esports, game jams
   - conference = talks, lectures, seminars, workshops, networking, hackathons
   - priroda   = outdoor events, hiking, parks, nature, markets, picnics
   - historia  = history tours, heritage sites, historical reenactments
   - zaujimave = anything interesting that doesn't clearly fit above

Rules:
- If unsure, use "zaujimave"
- Return ONLY valid JSON — no preamble, no explanation
- "description" must start with a single emoji character followed by a space and Slovak text
"""

USER_TEMPLATE = """\
Event details:
Title: {title}
Original description: {description}
Venue: {venue}
City: {city}
Date: {date}  Time: {time}

Return JSON: {{"description": "<emoji> <max 29 Slovak words>", "tag": "<one_tag>"}}
"""


def enrich_event(event: dict) -> dict:
    """Run ChatGPT enrichment and return a DB-ready dict for scraped_events table."""
    api_key = os.environ.get("OPENAI_API_KEY")
    description = ""
    tag = "zaujimave"

    if api_key:
        try:
            result = _call_gpt(
                title=event.get("title", ""),
                description=(event.get("original_description") or "")[:600],
                venue=event.get("venue", ""),
                city=event.get("city", ""),
                date=event.get("date", ""),
                time=event.get("time_start", ""),
            )
            description = result.get("description", "").strip()
            tag = result.get("tag", "zaujimave")
            if tag not in VALID_TAGS:
                tag = "zaujimave"
            # Safety: truncate if GPT exceeded word limit
            description = _enforce_word_limit(description, max_words=29)
        except Exception as e:
            logger.warning(f"ChatGPT enrichment failed for '{event.get('title', '')}': {e}")
            description = (event.get("original_description") or "")[:120]
            tag = "zaujimave"
    else:
        logger.warning("OPENAI_API_KEY not set — skipping enrichment")
        description = (event.get("original_description") or "")[:120]

    return {
        "source_url": event["source_url"],
        "source": event.get("source", "goout"),
        "title": event.get("title", "").strip(),
        "description": description,
        "tag": tag,
        "date": event.get("date") or None,
        "time_start": event.get("time_start") or None,
        "duration": event.get("duration", "") or None,
        "venue": event.get("venue", "") or None,
        "address": event.get("address", "") or None,
        "city": event.get("city", "") or None,
        "price": "Zadarmo",
        "photo_url": event.get("photo_url", "") or None,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "discord_sent": False,
        "approved": False,
        "rejected": False,
    }


def _call_gpt(title: str, description: str, venue: str, city: str, date: str, time: str) -> dict:
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    prompt = USER_TEMPLATE.format(
        title=title or "(no title)",
        description=description or "(no description available)",
        venue=venue or "(unknown venue)",
        city=city or "(unknown city)",
        date=date or "(unknown date)",
        time=time or "(unknown time)",
    )
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=150,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.5,
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)


def _enforce_word_limit(text: str, max_words: int) -> str:
    """Keep emoji prefix + max_words words."""
    if not text:
        return text
    parts = text.split(" ", 1)
    if len(parts) < 2:
        return text
    emoji_part = parts[0]
    rest = parts[1]
    words = rest.split()
    if len(words) <= max_words:
        return text
    return emoji_part + " " + " ".join(words[:max_words])
