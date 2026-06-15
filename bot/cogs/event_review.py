import asyncio
import logging
import os
import random
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

import discord
from discord.ext import commands, tasks

from config import DISCORD_CHANNEL_ID, POLL_INTERVAL, SUPABASE_URL, SUPABASE_KEY, BOT_USER_ID
from database import init_db, is_processed, add_pending_event, update_event_status
from instagram import post_to_instagram
from canva import create_event_image

logger = logging.getLogger(__name__)

COUNTRY_FLAG: dict[str, str] = {
    "SK": "🇸🇰",
    "AT": "🇦🇹",
    "CZ": "🇨🇿",
    "GB": "🇬🇧",
    "DE": "🇩🇪",
    "HU": "🇭🇺",
    "PL": "🇵🇱",
}

# Native language labels for embed fields per country
COUNTRY_LABELS: dict[str, dict[str, str]] = {
    "SK": {
        "date": "Dátum", "duration": "Trvanie", "venue": "Miesto",
        "address": "Adresa", "city": "Mesto", "free": "Vstup: Zadarmo",
        "price": "Vstup", "pay_at_door": "Platba na mieste",
    },
    "AT": {
        "date": "Datum", "duration": "Dauer", "venue": "Ort",
        "address": "Adresse", "city": "Stadt", "free": "Eintritt frei",
        "price": "Eintritt", "pay_at_door": "Zahlung vor Ort",
    },
    "CZ": {
        "date": "Datum", "duration": "Trvání", "venue": "Místo",
        "address": "Adresa", "city": "Město", "free": "Vstup zdarma",
        "price": "Vstup", "pay_at_door": "Platba na místě",
    },
    "GB": {
        "date": "Date", "duration": "Duration", "venue": "Venue",
        "address": "Address", "city": "City", "free": "Free Entry",
        "price": "Entry", "pay_at_door": "Pay at door",
    },
    "DE": {
        "date": "Datum", "duration": "Dauer", "venue": "Ort",
        "address": "Adresse", "city": "Stadt", "free": "Eintritt frei",
        "price": "Eintritt", "pay_at_door": "Zahlung vor Ort",
    },
}

# City name → country code fallback (for rows without country column)
CITY_COUNTRY: dict[str, str] = {
    "Bratislava": "SK", "Košice": "SK", "Kosice": "SK",
    "Vienna": "AT", "Wien": "AT",
    "Prague": "CZ", "Praha": "CZ",
    "London": "GB",
    "Berlin": "DE", "München": "DE", "Munich": "DE",
}


def _resolve_country(event: dict) -> str:
    country = event.get("country") or ""
    if country and country in COUNTRY_FLAG:
        return country
    return CITY_COUNTRY.get(event.get("city", ""), "SK")


TAG_EMOJI = {
    "Movement & Sport":       "🏃",
    "Wellness & Body":        "🧘",
    "Food & Drinks":          "🍽️",
    "Art & Creation":         "🎨",
    "Music & Nightlife":      "🎵",
    "Learning & Mind":        "📚",
    "Community & Belonging":  "🤝",
    # legacy fallback keys
    "coffee": "🍽️", "party": "🎵", "zapasy": "🏃", "sport": "🏃",
    "umenie": "🎨", "gaming": "🎨", "conference": "📚",
    "priroda": "🏃", "historia": "📚", "zaujimave": "🤝",
    "dancing": "🎵", "trhy": "🍽️",
}

TAG_COLOR = {
    "Movement & Sport":       discord.Color.from_rgb(0, 180, 80),
    "Wellness & Body":        discord.Color.from_rgb(100, 200, 180),
    "Food & Drinks":          discord.Color.from_rgb(220, 150, 50),
    "Art & Creation":         discord.Color.from_rgb(100, 0, 200),
    "Music & Nightlife":      discord.Color.from_rgb(180, 0, 180),
    "Learning & Mind":        discord.Color.from_rgb(0, 120, 180),
    "Community & Belonging":  discord.Color.from_rgb(200, 255, 0),
}

# Map legacy scraped tag values to new category names
OLD_TAG_TO_CATEGORY = {
    "sport": "Movement & Sport", "running": "Movement & Sport",
    "zapasy": "Movement & Sport", "priroda": "Movement & Sport",
    "dancing": "Movement & Sport", "tanec": "Movement & Sport",
    "yoga": "Wellness & Body", "joga": "Wellness & Body",
    "coffee": "Food & Drinks", "food": "Food & Drinks",
    "gastro": "Food & Drinks", "jedlo": "Food & Drinks",
    "trhy": "Food & Drinks", "markets": "Food & Drinks",
    "umenie": "Art & Creation", "art": "Art & Creation",
    "film": "Art & Creation", "gaming": "Art & Creation",
    "music": "Music & Nightlife", "hudba": "Music & Nightlife",
    "party": "Music & Nightlife",
    "tech": "Learning & Mind", "networking": "Learning & Mind",
    "conference": "Learning & Mind", "historia": "Learning & Mind",
    "zaujimave": "Community & Belonging",
}


def _build_ig_caption(event: dict) -> str:
    """Build Instagram caption for event post."""
    title = event.get("title", "")
    description = event.get("description", "")
    date = event.get("date", "")
    time_s = event.get("time_start", "")
    venue = event.get("venue", "")
    address = event.get("address", "")
    duration = event.get("duration", "")
    tag = event.get("tag", "zaujimave")
    emoji = TAG_EMOJI.get(tag, "✨")

    lines = [title]
    lines.append("")
    if description:
        lines.append(description)
        lines.append("")
    if date:
        date_line = f"📅 {date}"
        if time_s:
            date_line += f" o {time_s}"
        lines.append(date_line)
    if duration:
        lines.append(f"⏱ {duration}")
    if venue:
        lines.append(f"📍 {venue}")
    if address:
        lines.append(f"   {address}")
    lines.append("")
    lines.append(f"💚 Vstup zadarmo / Free entry")
    lines.append(f"{emoji} #{tag} #zadarmo #woeva")
    lines.append("")
    lines.append("👉 Stiahni Woeva app a pridaj sa!")
    lines.append("#slovensko #bratislava #events #freeevents #komunita")

    caption = "\n".join(lines)
    if len(caption) > 2190:
        caption = caption[:2187] + "..."
    return caption


def _duration_to_hours(duration_str: str) -> float:
    """Convert '2h 30min' -> 2.5, '90min' -> 1.5, '2h' -> 2.0"""
    if not duration_str:
        return 2.0
    hours = 0
    minutes = 0
    h_match = re.search(r"(\d+)h", duration_str)
    m_match = re.search(r"(\d+)min", duration_str)
    if h_match:
        hours = int(h_match.group(1))
    if m_match:
        minutes = int(m_match.group(1))
    total = hours + minutes / 60
    return total if total > 0 else 2.0


async def _add_fake_attendees_staggered(event_id: str, user_ids: list[str]):
    """Add fake attendees one by one with random delays (5 min – 3 hours apart)."""
    from supabase import create_client
    for i, uid in enumerate(user_ids):
        # First one: 5–45 min, subsequent: 30 min – 3 h
        delay = random.randint(5, 30) if i == 0 else random.randint(1800, 10800)
        await asyncio.sleep(delay)
        try:
            db = create_client(SUPABASE_URL, SUPABASE_KEY)
            db.table("event_attendees").insert({"event_id": event_id, "user_id": uid, "paid": True}).execute()
            db.table("events").update({"going_count": i + 1}).eq("id", event_id).execute()
            logger.info(f"Fake attendee {uid[:8]} added to event {event_id[:8]} (slot {i+1})")
        except Exception as e:
            logger.warning(f"Fake attendee insert failed: {e}")


async def _geocode(address: str) -> tuple:
    """Geocode address using Google Geocoding API. Returns (lat, lng) or (None, None)."""
    if not address:
        return None, None
    try:
        import httpx as _httpx
        params = {"address": address, "key": "AIzaSyDhtj5z-Us7Ic01tHuaJxC_mLTZPG3moj0", "language": "sk"}
        async with _httpx.AsyncClient(timeout=8) as hc:
            r = await hc.get("https://maps.googleapis.com/maps/api/geocode/json", params=params)
            data = r.json()
            if data.get("results"):
                loc = data["results"][0]["geometry"]["location"]
                return loc["lat"], loc["lng"]
    except Exception as e:
        logger.warning(f"Geocode failed for '{address}': {e}")
    return None, None


async def _publish_event(event: dict, post_ig: bool) -> str:
    """Copy approved event to Woeva events table. Optionally post to Instagram."""
    from supabase import create_client
    db = create_client(SUPABASE_URL, SUPABASE_KEY)

    event_date = event.get("date") or None
    event_time = event.get("time_start") or None
    duration_hours = _duration_to_hours(event.get("duration", ""))

    venue_full = event.get("venue") or ""
    if event.get("address"):
        venue_full = f"{venue_full}, {event['address']}".strip(", ")

    # Geocode if lat/lng missing
    lat = event.get("lat") or None
    lng = event.get("lng") or None
    if not lat or not lng:
        query = venue_full or event.get("city") or ""
        if query:
            lat, lng = await _geocode(query)
            if lat:
                logger.info(f"Geocoded '{query}' → {lat}, {lng}")

    # Insert into Woeva events table
    raw_price = event.get("price") or 0
    try:
        price = float(raw_price) if str(raw_price).replace('.', '', 1).isdigit() else 0.0
    except (ValueError, TypeError):
        price = 0.0
    pay_at_door = bool(event.get("pay_at_door", False))
    is_free = price == 0 and not pay_at_door

    # Fetch Woeva Picks club ID (search by name only — creator_id may vary after club merge)
    _wpc_res = db.table("clubs").select("id").ilike("name", "%woeva picks%").order("created_at", desc=False).limit(1).execute()
    woeva_picks_club_id = (_wpc_res.data[0]["id"] if _wpc_res and _wpc_res.data else None)

    event_row = {
        "creator_id": BOT_USER_ID,
        "club_id": woeva_picks_club_id,
        "title": event.get("title", ""),
        "tagline": event.get("description", ""),
        "category": OLD_TAG_TO_CATEGORY.get(event.get("tag", ""), event.get("tag", "Community & Belonging")),
        "cover_url": event.get("photo_url") or None,
        "date": event_date,
        "time": event_time,
        "duration": duration_hours,
        "venue": venue_full or None,
        "lat": lat,
        "lng": lng,
        "price": price,
        "going_count": 0,
        "is_free": is_free,
        "pay_at_door": pay_at_door,
        "city": event.get("city") or "Slovensko",
        "country": _resolve_country(event),
        "is_recurring": event.get("is_recurring", False),
        "recurring_end_date": event.get("recurring_end_date") or None,
        "source": event.get("source") or "scraped",
    }

    # Use service role (bypasses RLS) via the provided SUPABASE_KEY
    result = db.table("events").insert(event_row).execute()

    # Add fake attendees for social proof (Slovak events only, staggered delays)
    try:
        fake_ids_raw = os.environ.get("BOT_FAKE_USER_IDS", "")
        country = event_row.get("country", "SK")
        if fake_ids_raw and result.data and country == "SK":
            event_id = result.data[0]["id"]
            fake_ids = [x.strip() for x in fake_ids_raw.split(",") if x.strip()]
            # First 16 have photos, last 9 don't (matches create_bots.py order)
            with_photo = fake_ids[:16]
            no_photo = fake_ids[16:]
            count = random.choices([0, 0, 1, 1, 1, 2, 2, 3], k=1)[0]
            if count > 0:
                if count == 1:
                    chosen = random.sample(fake_ids, 1)
                elif count == 2:
                    # 1 with photo + 1 without
                    chosen = random.sample(with_photo, 1) + random.sample(no_photo, 1)
                else:
                    # 2 with photo + 1 without
                    chosen = random.sample(with_photo, 2) + random.sample(no_photo, 1)
                random.shuffle(chosen)
                asyncio.ensure_future(_add_fake_attendees_staggered(event_id, chosen))
    except Exception as e:
        logger.warning(f"Fake attendees failed: {e}")

    # Mark as approved in scraped_events
    db.table("scraped_events").update({"approved": True}).eq("id", event["supabase_id"]).execute()


    if not post_ig:
        return "✅ Pridane do appky"

    # Instagram post
    try:
        import httpx as _httpx
        photo_url = event.get("photo_url", "")
        if not photo_url:
            raise ValueError("No photo URL — Instagram post skipped")

        async with _httpx.AsyncClient(timeout=60, follow_redirects=True) as hc:
            img_resp = await hc.get(photo_url, headers={"User-Agent": "Mozilla/5.0"})
            img_resp.raise_for_status()
            photo_bytes = img_resp.content

        img_bytes = create_event_image(photo_bytes)
        caption = _build_ig_caption(event)
        await post_to_instagram(img_bytes, caption)
        return "✅ App + Instagram"

    except _httpx.ReadTimeout:
        return "✅ App OK — Instagram: rate limit, skus znova za 30 min"
    except Exception as e:
        import traceback
        logger.error(f"Instagram failed for {event['supabase_id']}: {e!r}\n{traceback.format_exc()}")
        return f"✅ App OK — Instagram failed: {e}"


# ── Edit modal ───────────────────────────────────────────────────────────────

VALID_TAGS = list(TAG_EMOJI.keys())

class EditEventModal(discord.ui.Modal, title="Upraviť event"):
    def __init__(self, view: "EventConfirmView"):
        super().__init__()
        self.event_view = view
        event = view.event

        self.f_title = discord.ui.TextInput(
            label="Názov", default=event.get("title", ""), max_length=200, required=True
        )
        self.f_tag = discord.ui.TextInput(
            label="Kategória",
            default=OLD_TAG_TO_CATEGORY.get(event.get("tag", ""), event.get("tag", "Community & Belonging")),
            placeholder="Movement & Sport", required=False, max_length=60
        )
        self.f_date = discord.ui.TextInput(
            label="Dátum (YYYY-MM-DD)", default=event.get("date", ""),
            placeholder="2026-06-01", required=False, max_length=10
        )
        self.f_time = discord.ui.TextInput(
            label="Čas (HH:MM)", default=event.get("time_start", ""),
            placeholder="18:00", required=False, max_length=5
        )
        price_default = str(int(event.get("price") or 0)) if event.get("price") else ""
        if event.get("pay_at_door") and price_default:
            price_default = f"{price_default} pad"
        self.f_price = discord.ui.TextInput(
            label="Cena € (napr. '5' alebo '5 pad' = na mieste)",
            default=price_default,
            placeholder="prázdne = zadarmo | 5 = 5€ | 5 pad = 5€ na mieste",
            required=False, max_length=20
        )
        for field in [self.f_title, self.f_tag, self.f_date, self.f_time, self.f_price]:
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        e = self.event_view.event
        e["title"] = self.f_title.value.strip()
        e["tag"] = self.f_tag.value.strip() or "Community & Belonging"
        e["date"] = self.f_date.value.strip()
        e["time_start"] = self.f_time.value.strip()

        # Parse price field: "5 pad" → price=5, pay_at_door=True; "5" → price=5; "0"/"" → free
        raw_price = self.f_price.value.strip().lower()
        pay_at_door = "pad" in raw_price
        price_str = raw_price.replace("pad", "").strip()
        try:
            e["price"] = float(price_str) if price_str else 0.0
        except ValueError:
            e["price"] = 0.0
        e["pay_at_door"] = pay_at_door

        # Update embed to reflect changes
        if interaction.message and interaction.message.embeds:
            old = interaction.message.embeds[0]
            country = _resolve_country(e)
            flag = COUNTRY_FLAG.get(country, "🌍")
            lbl = COUNTRY_LABELS.get(country, COUNTRY_LABELS["SK"])
            tag = e.get("tag", "zaujimave")
            emoji = TAG_EMOJI.get(tag, "✨")

            desc_lines = []
            if e.get("description"):
                desc_lines.append(e["description"])
            if e.get("date"):
                d_line = f"\n**{lbl['date']}:** {e['date']}"
                if e.get("time_start"):
                    d_line += f" o {e['time_start']}"
                desc_lines.append(d_line)
            if e.get("venue"):
                desc_lines.append(f"**{lbl['venue']}:** {e['venue']}")
            if e.get("city"):
                desc_lines.append(f"**{lbl['city']}:** {e['city']}")
            price = e.get("price") or 0
            pay_at_door = e.get("pay_at_door", False)
            if pay_at_door and price:
                desc_lines.append(f"\n**{lbl['pay_at_door']}:** {price:.0f}€")
            elif price:
                desc_lines.append(f"\n**{lbl['price']}:** {price:.0f}€")
            else:
                desc_lines.append(f"\n**{lbl['free']}**")

            emb = old.copy()
            emb.title = e['title'][:256]
            emb.description = "\n".join(desc_lines)
            emb.set_footer(text=f"✏️ Upravené — {interaction.user.display_name}")

        await interaction.response.send_message("✏️ Event upravený.", ephemeral=True)
        if interaction.message and interaction.message.embeds:
            await interaction.message.edit(embed=emb)


# ── Per-event confirmation view ─────────────────────────────────────────────

class EventConfirmView(discord.ui.View):
    """Shown for each event — 4 buttons: Edit / Instagram+App / App only / Reject."""

    def __init__(self, event: dict):
        super().__init__(timeout=None)
        self.event = event
        sid = event["supabase_id"]

        b_edit = discord.ui.Button(
            label="Upraviť", emoji="✏️",
            style=discord.ButtonStyle.secondary,
            custom_id=f"wv_edit:{sid}",
        )
        b_ig = discord.ui.Button(
            label="Instagram + App", emoji="🚀",
            style=discord.ButtonStyle.success,
            custom_id=f"wv_ig:{sid}",
        )
        b_app = discord.ui.Button(
            label="Iba App", emoji="📱",
            style=discord.ButtonStyle.primary,
            custom_id=f"wv_app:{sid}",
        )
        b_skip = discord.ui.Button(
            label="Zahodit", emoji="❌",
            style=discord.ButtonStyle.danger,
            custom_id=f"wv_skip:{sid}",
        )

        b_edit.callback = self._cb_edit
        b_ig.callback = self._cb_ig
        b_app.callback = self._cb_app
        b_skip.callback = self._cb_skip

        for b in [b_edit, b_ig, b_app, b_skip]:
            self.add_item(b)

    async def _finish(self, interaction: discord.Interaction, post_ig: bool):
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            result = await _publish_event(self.event, post_ig=post_ig)
            await update_event_status(self.event["supabase_id"], "approved")
            if interaction.message and interaction.message.embeds:
                emb = interaction.message.embeds[0].copy()
                emb.colour = discord.Color.green()
                emb.set_footer(text=f"{result} — {interaction.user.display_name}")
                await interaction.message.edit(embed=emb, view=None)
            await interaction.followup.send(result, ephemeral=True)
        except Exception as e:
            logger.error(f"Publish error: {e}", exc_info=True)
            await interaction.followup.send(f"Chyba: {e}", ephemeral=True)

    async def _cb_edit(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EditEventModal(self))

    async def _cb_ig(self, interaction: discord.Interaction):
        await self._finish(interaction, post_ig=True)

    async def _cb_app(self, interaction: discord.Interaction):
        await self._finish(interaction, post_ig=False)

    async def _cb_skip(self, interaction: discord.Interaction):
        await update_event_status(self.event["supabase_id"], "rejected")
        try:
            from supabase import create_client
            db = create_client(SUPABASE_URL, SUPABASE_KEY)
            db.table("scraped_events").update({"rejected": True}).eq("id", self.event["supabase_id"]).execute()
        except Exception as e:
            logger.error(f"Failed to mark rejected in Supabase: {e}")
        if interaction.message and interaction.message.embeds:
            emb = interaction.message.embeds[0].copy()
            emb.colour = discord.Color.red()
            emb.set_footer(text=f"Zahodene — {interaction.user.display_name}")
            await interaction.message.edit(embed=emb, view=None)
        await interaction.response.send_message("Event zahodeny.", ephemeral=True)


# ── Cog ─────────────────────────────────────────────────────────────────────

NO_EVENTS_INTERVAL_H = 6  # send "no events" notice at most once per this many hours


class EventReviewCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._picks_lock = asyncio.Lock()
        self._last_event_sent: datetime = datetime.now(timezone.utc)
        self._last_no_events_notice: Optional[datetime] = None
        self.poll_events.start()
        self.poll_picks.start()

    def cog_unload(self):
        self.poll_events.cancel()
        self.poll_picks.cancel()

    async def cog_load(self):
        """On startup: re-register persistent views for all pending events."""
        try:
            from supabase import create_client
            db = create_client(SUPABASE_URL, SUPABASE_KEY)
            result = (
                db.table("scraped_events")
                .select("id, title, description, tag, date, time_start, duration, venue, address, city, country, photo_url, source_url, source, price")
                .eq("discord_sent", True)
                .eq("approved", False)
                .eq("rejected", False)
                .order("scraped_at", desc=True)
                .limit(200)
                .execute()
            )
            restored = 0
            for row in result.data or []:
                event = _row_to_event(row)
                self.bot.add_view(EventConfirmView(event))
                restored += 1

            if restored:
                logger.info(f"Restored {restored} persistent EventConfirmView(s)")
        except Exception as e:
            logger.warning(f"Could not restore views on startup: {e}")

    @tasks.loop(seconds=POLL_INTERVAL)
    async def poll_events(self):
        async with self._lock:
            await self._check_new_events()

    @poll_events.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(15)

    @tasks.loop(seconds=10)
    async def poll_picks(self):
        """Fast loop — checks only woeva_picks events every 10 seconds."""
        async with self._picks_lock:
            await self._check_picks_events()

    @poll_picks.before_loop
    async def before_poll_picks(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(10)

    async def _check_picks_events(self):
        if not SUPABASE_URL or not SUPABASE_KEY:
            return
        try:
            from supabase import create_client
            db = create_client(SUPABASE_URL, SUPABASE_KEY)
            res = (
                db.table("scraped_events")
                .select("id, title, description, tag, date, time_start, duration, venue, address, city, country, photo_url, source_url, source, price")
                .in_("source", ["instagram", "woeva_picks"])
                .eq("approved", False)
                .eq("rejected", False)
                .neq("discord_sent", True)
                .order("scraped_at", desc=False)
                .limit(5)
                .execute()
            )
            rows = res.data or []
            logger.info(f"Picks poll: found {len(rows)} picks events")
            if not rows:
                return
            channel = self.bot.get_channel(DISCORD_CHANNEL_ID)
            if not channel:
                logger.error(f"Picks poll: channel {DISCORD_CHANNEL_ID} not found")
                return
            self._last_event_sent = datetime.now(timezone.utc)
            self._last_no_events_notice = None
            for row in rows:
                sid = str(row["id"])
                logger.info(f"Picks: processing '{row.get('title')}' id={sid[:8]}")
                if await is_processed(sid):
                    logger.info(f"Picks: already processed {sid[:8]}, skipping")
                    db.table("scraped_events").update({"discord_sent": True}).eq("id", row["id"]).execute()
                    continue
                try:
                    event = _row_to_event(row)
                except Exception as parse_err:
                    logger.error(f"Picks: _row_to_event FAILED for '{row.get('title')}' id={sid[:8]}: {parse_err} — marking discord_sent to unblock queue")
                    db.table("scraped_events").update({"discord_sent": True}).eq("id", row["id"]).execute()
                    await add_pending_event(sid, "parse_error", "0")
                    continue
                try:
                    await self._send_event(channel, event)
                    db.table("scraped_events").update({"discord_sent": True}).eq("id", row["id"]).execute()
                    logger.info(f"Picks: sent '{row['title']}' to Discord OK")
                except Exception as send_err:
                    logger.error(f"Picks: SEND FAILED for '{row['title']}': {send_err}", exc_info=True)
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Picks poll error: {e}", exc_info=True)

    async def _check_new_events(self):
        if not SUPABASE_URL or not SUPABASE_KEY:
            return
        try:
            from supabase import create_client
            from datetime import date as _date
            db = create_client(SUPABASE_URL, SUPABASE_KEY)
            _select = "id, title, description, tag, date, time_start, duration, venue, address, city, country, photo_url, source_url, source, price"
            today = _date.today().isoformat()
            # First: mark stale past-dated events as sent so they leave the queue
            db.table("scraped_events").update({"discord_sent": True}).or_("discord_sent.is.null,discord_sent.eq.false").lt("date", today).eq("approved", False).eq("rejected", False).neq("source", "instagram").neq("source", "woeva_picks").execute()
            # Rebuild query separately to avoid supabase-py mutating shared builder
            new_events = (
                db.table("scraped_events")
                .select(_select)
                .or_("discord_sent.is.null,discord_sent.eq.false")
                .eq("approved", False)
                .eq("rejected", False)
                .neq("source", "instagram")
                .neq("source", "woeva_picks")
                .or_(f"date.is.null,date.gte.{today}")
                .order("scraped_at", desc=False)
                .limit(20)
                .execute()
            ).data or []
            logger.info(f"Poll: found {len(new_events)} unclaimed scraped events")

            channel = self.bot.get_channel(DISCORD_CHANNEL_ID)
            if not channel:
                logger.error(f"Channel {DISCORD_CHANNEL_ID} not found")
                return

            if not new_events:
                await self._maybe_send_no_events_notice(channel)
                return

            self._last_event_sent = datetime.now(timezone.utc)
            self._last_no_events_notice = None

            for row in new_events:
                sid = str(row["id"])
                if await is_processed(sid):
                    # Already sent — just mark claimed so it leaves the queue
                    db.table("scraped_events").update({"discord_sent": True}).eq("id", row["id"]).execute()
                    continue
                try:
                    event = _row_to_event(row)
                    await self._send_event(channel, event)
                    db.table("scraped_events").update({"discord_sent": True}).eq("id", row["id"]).execute()
                except Exception as row_err:
                    logger.error(f"Failed to send event '{row.get('title')}' ({sid[:8]}): {row_err} — skipping")
                    db.table("scraped_events").update({"discord_sent": True}).eq("id", row["id"]).execute()
                await asyncio.sleep(0.5)

            logger.info(f"Sent {len(new_events)} events to Discord")

        except Exception as e:
            logger.error(f"Poll error: {e}", exc_info=True)

    async def _maybe_send_no_events_notice(self, channel: discord.TextChannel):
        """Send a 'no new events found' notice at most once every NO_EVENTS_INTERVAL_H hours."""
        now = datetime.now(timezone.utc)
        interval = timedelta(hours=NO_EVENTS_INTERVAL_H)

        # Don't send if we just sent real events recently
        if (now - self._last_event_sent) < interval:
            return
        # Don't repeat the notice within the same interval
        if self._last_no_events_notice and (now - self._last_no_events_notice) < interval:
            return

        self._last_no_events_notice = now
        local_time = now.astimezone(ZoneInfo("Europe/Bratislava")).strftime("%H:%M")
        await channel.send(
            f"🔍 **Scraper hlásenie ({local_time})** — nenašiel som žiadne nové eventy."
        )
        logger.info("Sent no-events notice to Discord")

    async def _send_event(self, channel: discord.TextChannel, event: dict):
        tag = (event.get("tag") or "zaujimave").split(",")[0].strip()
        emoji = TAG_EMOJI.get(tag, "✨")
        country = _resolve_country(event)
        color = discord.Color.from_rgb(200, 255, 0) if country == "SK" else TAG_COLOR.get(tag, discord.Color.from_rgb(200, 255, 0))
        flag = COUNTRY_FLAG.get(country, "🌍")
        lbl = COUNTRY_LABELS.get(country, COUNTRY_LABELS["SK"])

        source_url = event.get("source_url") or ""
        emb = discord.Embed(
            title=(event.get('title') or '')[:256],
            url=source_url if source_url.startswith("http") else None,
            color=color,
        )

        desc_lines = []
        if event.get("description"):
            desc_lines.append(event["description"])
        if event.get("date"):
            d_line = f"\n**{lbl['date']}:** {event['date']}"
            if event.get("time_start"):
                d_line += f" o {event['time_start']}"
            desc_lines.append(d_line)
        if event.get("duration"):
            desc_lines.append(f"**{lbl['duration']}:** {event['duration']}")
        if event.get("venue"):
            desc_lines.append(f"**{lbl['venue']}:** {event['venue']}")
        if event.get("address"):
            desc_lines.append(f"**{lbl['address']}:** {event['address']}")
        if event.get("city"):
            desc_lines.append(f"**{lbl['city']}:** {event['city']}")
        price_num = _safe_float(event.get("price"))
        pay_at_door = event.get("pay_at_door", False)
        if pay_at_door and price_num > 0:
            desc_lines.append(f"\n**{lbl['pay_at_door']}:** {price_num:.0f}€")
        elif price_num > 0:
            desc_lines.append(f"\n**{lbl['price']}:** {price_num:.0f}€")
        else:
            desc_lines.append(f"\n**{lbl['free']}**")

        emb.description = "\n".join(desc_lines)
        emb.add_field(name="Tag", value=tag, inline=True)
        emb.add_field(name="Zdroj", value=(event.get("source") or "").upper(), inline=True)
        emb.add_field(name="Krajina", value=f"{flag} {country}", inline=True)
        if source_url.startswith("http") and event.get("source") == "nitra":
            emb.add_field(name="🔗 Link", value=source_url, inline=False)

        if event.get("photo_url") and str(event["photo_url"]).startswith("http"):
            emb.set_image(url=event["photo_url"])
        emb.set_footer(text=f"ID: {event['supabase_id']}")

        view = EventConfirmView(event)
        self.bot.add_view(view)
        is_sk = country == "SK"
        header = "🇸🇰 **SLOVENSKÝ EVENT — čo s ním?**" if is_sk else "**Nový event — čo s ním?**"
        msg = await channel.send(
            content=header,
            embed=emb,
            view=view,
        )
        await add_pending_event(event["supabase_id"], str(msg.id), str(channel.id))
        logger.info(f"Sent event {event['supabase_id'][:8]} to Discord msg {msg.id}")


def _safe_float(val) -> float:
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


def _row_to_event(row: dict) -> dict:
    return {
        "supabase_id": str(row["id"]),
        "title": row.get("title") or "",
        "description": row.get("description") or "",
        "tag": row.get("tag") or "zaujimave",
        "date": row.get("date") or "",
        "time_start": row.get("time_start") or "",
        "duration": row.get("duration") or "",
        "venue": row.get("venue") or "",
        "address": row.get("address") or "",
        "city": row.get("city") or "",
        "country": row.get("country") or "",
        "photo_url": row.get("photo_url") or "",
        "source_url": row.get("source_url") or "",
        "source": row.get("source") or "",
        "lat": row.get("lat"),
        "lng": row.get("lng"),
        "is_recurring": row.get("is_recurring", False),
        "recurring_end_date": row.get("recurring_end_date") or None,
        "price": row.get("price") or 0,
        "pay_at_door": bool(row.get("pay_at_door") or (_safe_float(row.get("price")) > 0 and row.get("source") == "woeva_picks")),
    }


async def setup(bot: commands.Bot):
    await bot.add_cog(EventReviewCog(bot))
