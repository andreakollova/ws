import asyncio
import logging
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
    },
    "AT": {
        "date": "Datum", "duration": "Dauer", "venue": "Ort",
        "address": "Adresse", "city": "Stadt", "free": "Eintritt frei",
    },
    "CZ": {
        "date": "Datum", "duration": "Trvání", "venue": "Místo",
        "address": "Adresa", "city": "Město", "free": "Vstup zdarma",
    },
    "GB": {
        "date": "Date", "duration": "Duration", "venue": "Venue",
        "address": "Address", "city": "City", "free": "Free Entry",
    },
    "DE": {
        "date": "Datum", "duration": "Dauer", "venue": "Ort",
        "address": "Adresse", "city": "Stadt", "free": "Eintritt frei",
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
    "coffee": "☕",
    "party": "🎉",
    "zapasy": "🏆",
    "sport": "🏃",
    "umenie": "🎨",
    "gaming": "🎮",
    "conference": "📋",
    "priroda": "🌿",
    "historia": "🏛️",
    "zaujimave": "✨",
}

TAG_COLOR = {
    "coffee":     discord.Color.from_rgb(150, 90, 50),
    "party":      discord.Color.from_rgb(180, 0, 180),
    "zapasy":     discord.Color.from_rgb(255, 165, 0),
    "sport":      discord.Color.from_rgb(0, 180, 80),
    "umenie":     discord.Color.from_rgb(100, 0, 200),
    "gaming":     discord.Color.from_rgb(0, 100, 255),
    "conference": discord.Color.from_rgb(0, 120, 180),
    "priroda":    discord.Color.from_rgb(50, 160, 50),
    "historia":   discord.Color.from_rgb(120, 80, 20),
    "zaujimave":  discord.Color.from_rgb(200, 255, 0),
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


async def _publish_event(event: dict, post_ig: bool) -> str:
    """Copy approved event to Woeva events table. Optionally post to Instagram."""
    from supabase import create_client
    db = create_client(SUPABASE_URL, SUPABASE_KEY)

    event_date = event.get("date") or None
    event_time = event.get("time_start") or "00:00"
    duration_hours = _duration_to_hours(event.get("duration", ""))

    venue_full = event.get("venue") or ""
    if event.get("address"):
        venue_full = f"{venue_full}, {event['address']}".strip(", ")

    # Insert into Woeva events table
    event_row = {
        "creator_id": BOT_USER_ID,
        "title": event.get("title", ""),
        "tagline": event.get("description", ""),
        "category": event.get("tag", "zaujimave"),
        "cover_url": event.get("photo_url") or None,
        "date": event_date,
        "time": event_time,
        "duration": duration_hours,
        "venue": venue_full or None,
        "lat": None,
        "lng": None,
        "price": 0,
        "going_count": 0,
        "is_free": True,
        "city": event.get("city") or "Slovensko",
        "country": _resolve_country(event),
    }

    # Use service role (bypasses RLS) via the provided SUPABASE_KEY
    db.table("events").insert(event_row).execute()

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

class EditEventModal(discord.ui.Modal, title="Upraviť event"):
    def __init__(self, view: "EventConfirmView"):
        super().__init__()
        self.event_view = view
        event = view.event

        self.f_title = discord.ui.TextInput(
            label="Názov", default=event.get("title", ""), max_length=200, required=True
        )
        self.f_date = discord.ui.TextInput(
            label="Dátum (YYYY-MM-DD)", default=event.get("date", ""),
            placeholder="2026-06-01", required=False, max_length=10
        )
        self.f_time = discord.ui.TextInput(
            label="Čas (HH:MM)", default=event.get("time_start", ""),
            placeholder="18:00", required=False, max_length=5
        )
        self.f_venue = discord.ui.TextInput(
            label="Miesto", default=event.get("venue", ""),
            placeholder="Názov miesta", required=False, max_length=200
        )
        self.f_city = discord.ui.TextInput(
            label="Mesto", default=event.get("city", "Bratislava"),
            required=False, max_length=100
        )
        for field in [self.f_title, self.f_date, self.f_time, self.f_venue, self.f_city]:
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        e = self.event_view.event
        e["title"] = self.f_title.value.strip()
        e["date"] = self.f_date.value.strip()
        e["time_start"] = self.f_time.value.strip()
        e["venue"] = self.f_venue.value.strip()
        e["city"] = self.f_city.value.strip()

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
            desc_lines.append(f"\n**{lbl['free']}**")

            emb = old.copy()
            emb.title = e['title'][:256]
            emb.description = "\n".join(desc_lines)
            emb.set_footer(text=f"✏️ Upravené — {interaction.user.display_name}")
            await interaction.message.edit(embed=emb)

        await interaction.response.send_message("✏️ Event upravený.", ephemeral=True)


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
        """On startup: re-register views AND resend any claimed-but-unsent events."""
        try:
            from supabase import create_client
            db = create_client(SUPABASE_URL, SUPABASE_KEY)
            result = (
                db.table("scraped_events")
                .select("id, title, description, tag, date, time_start, duration, venue, address, city, country, photo_url, source_url, source")
                .eq("discord_sent", True)
                .eq("approved", False)
                .eq("rejected", False)
                .order("scraped_at", desc=True)
                .limit(100)
                .execute()
            )
            restored = 0
            to_resend = []
            for row in result.data or []:
                event = _row_to_event(row)
                self.bot.add_view(EventConfirmView(event))
                restored += 1
                # If not in local SQLite, this event was claimed but never sent — queue for resend
                if not await is_processed(str(row["id"])):
                    to_resend.append(row)

            if restored:
                logger.info(f"Restored {restored} persistent EventConfirmView(s), {len(to_resend)} need resend")

            if to_resend:
                asyncio.ensure_future(self._resend_stuck_events(to_resend))
        except Exception as e:
            logger.warning(f"Could not restore views on startup: {e}")

    async def _resend_stuck_events(self, rows: list[dict]):
        """Resend events that were claimed (discord_sent=True) but never actually sent."""
        await self.bot.wait_until_ready()
        await asyncio.sleep(5)
        channel = self.bot.get_channel(DISCORD_CHANNEL_ID)
        if not channel:
            logger.error(f"Resend: channel {DISCORD_CHANNEL_ID} not found")
            return
        for row in rows:
            try:
                event = _row_to_event(row)
                await self._send_event(channel, event)
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Resend failed for {row['id']}: {e}")

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
        """Fast loop — checks only instagram/picks events every 10 seconds."""
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
                .select("id, title, description, tag, date, time_start, duration, venue, address, city, country, photo_url, source_url, source")
                .eq("source", "instagram")
                .eq("approved", False)
                .eq("rejected", False)
                .neq("discord_sent", True)
                .order("scraped_at", desc=False)
                .limit(5)
                .execute()
            )
            rows = res.data or []
            logger.info(f"Picks poll: found {len(rows)} instagram events")
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
                event = _row_to_event(row)
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
            db = create_client(SUPABASE_URL, SUPABASE_KEY)
            _select = "id, title, description, tag, date, time_start, duration, venue, address, city, country, photo_url, source_url, source"
            # Rebuild query separately to avoid supabase-py mutating shared builder
            new_events = (
                db.table("scraped_events")
                .select(_select)
                .or_("discord_sent.is.null,discord_sent.eq.false")
                .eq("approved", False)
                .eq("rejected", False)
                .neq("source", "instagram")
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
                event = _row_to_event(row)
                await self._send_event(channel, event)
                # Mark claimed AFTER successful send
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
        color = TAG_COLOR.get(tag, discord.Color.from_rgb(200, 255, 0))

        country = _resolve_country(event)
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
        desc_lines.append(f"\n**{lbl['free']}**")

        emb.description = "\n".join(desc_lines)
        emb.add_field(name="Tag", value=tag, inline=True)
        emb.add_field(name="Zdroj", value=(event.get("source") or "").upper(), inline=True)
        emb.add_field(name="Krajina", value=f"{flag} {country}", inline=True)

        if event.get("photo_url"):
            emb.set_image(url=event["photo_url"])
        emb.set_footer(text=f"ID: {event['supabase_id']}")

        view = EventConfirmView(event)
        self.bot.add_view(view)
        msg = await channel.send(
            content="**Novy event — co s nim?**",
            embed=emb,
            view=view,
        )
        await add_pending_event(event["supabase_id"], str(msg.id), str(channel.id))
        logger.info(f"Sent event {event['supabase_id'][:8]} to Discord msg {msg.id}")


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
    }


async def setup(bot: commands.Bot):
    await bot.add_cog(EventReviewCog(bot))
