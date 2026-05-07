import asyncio
import logging

import discord
from discord.ext import commands

from config import DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID
from database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/tmp/woeva_bot.log"),
    ],
)
logger = logging.getLogger(__name__)


class WoevaBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await init_db()
        await self.load_extension("cogs.event_review")
        logger.info("Extensions loaded")

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} ({self.user.id})")
        ch = self.get_channel(DISCORD_CHANNEL_ID)
        if ch:
            logger.info(f"Review channel: #{ch.name} ({ch.id})")
        else:
            logger.warning(f"Review channel NOT FOUND — check DISCORD_CHANNEL_ID={DISCORD_CHANNEL_ID}")

    async def on_message(self, message: discord.Message):
        if message.author == self.user:
            return
        is_dm = isinstance(message.channel, discord.DMChannel)
        is_mention = self.user in message.mentions
        if not is_dm and not is_mention:
            return

        text = message.content.lower().strip()
        text = text.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "").strip()

        if any(w in text for w in ["ahoj", "hello", "hi", "hey"]):
            reply = f"Ahoj {message.author.display_name}! Som Woeva bot — scrapujem zadarmo eventy na Slovensku."
        elif "help" in text or "pomoc" in text:
            reply = (
                "**Co robim:**\n"
                "Scrapujem zadarmo eventy z GoOut a Tootoot 4x denne.\n"
                "Ked najdem novy event, poslem ho sem na schvalenie.\n\n"
                "**Tlacidla:**\n"
                "Instagramm + App — pridame do appky a postnem na Instagram\n"
                "Iba App — pridame iba do appky\n"
                "Zahodit — event zahodime\n"
            )
        else:
            reply = f"Ahoj {message.author.display_name}! Napisz `help` pre viac info."

        await message.reply(reply)
        await self.process_commands(message)


async def main():
    async with WoevaBot() as bot:
        await bot.start(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
