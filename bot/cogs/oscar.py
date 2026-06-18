import discord
from discord.ext import commands, tasks
from datetime import time, timezone
import os
import aiohttp
import json
import logging

logger = logging.getLogger(__name__)

OSCAR_APP_URL = os.environ.get('OSCAR_APP_URL', '').rstrip('/')
OSCAR_PASSWORD = os.environ.get('OSCAR_PASSWORD', '')
INTERNAL_SECRET = os.environ.get('INTERNAL_SECRET', '')
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')
IG_USER_ID = os.environ.get('INSTAGRAM_ACCOUNT_ID', '')
IG_ACCESS_TOKEN = os.environ.get('INSTAGRAM_ACCESS_TOKEN', '')


async def _delete_storage_file(session: aiohttp.ClientSession, url: str | None):
    """Delete a file from Supabase Storage given its public URL."""
    if not url or not SUPABASE_URL:
        return
    # URL format: https://<project>.supabase.co/storage/v1/object/public/<bucket>/<path>
    marker = '/storage/v1/object/public/'
    idx = url.find(marker)
    if idx == -1:
        return
    after = url[idx + len(marker):]
    parts = after.split('/', 1)
    if len(parts) != 2:
        return
    bucket, file_path = parts[0], parts[1].split('?')[0]
    try:
        async with session.delete(
            f'{SUPABASE_URL}/storage/v1/object/{bucket}',
            headers={
                'apikey': SUPABASE_KEY,
                'Authorization': f'Bearer {SUPABASE_KEY}',
                'Content-Type': 'application/json',
            },
            data=json.dumps({'prefixes': [file_path]}),
        ) as res:
            logger.info(f'Storage delete {bucket}/{file_path}: {res.status}')
    except Exception as e:
        logger.warning(f'Storage delete failed for {url}: {e}')


class Oscar(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.oscar_lifestyle_cron.start()
        self.oscar_animation_cron.start()

    def cog_unload(self):
        self.oscar_lifestyle_cron.cancel()
        self.oscar_animation_cron.cancel()

    @tasks.loop(time=time(7, 15, tzinfo=timezone.utc))
    async def oscar_lifestyle_cron(self):
        if not OSCAR_APP_URL or not OSCAR_PASSWORD:
            return
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f'{OSCAR_APP_URL}/api/cron',
                headers={'x-password': OSCAR_PASSWORD},
            ) as res:
                logger.info(f'Oscar lifestyle cron: {res.status}')

    @tasks.loop(time=time(13, 0, tzinfo=timezone.utc))
    async def oscar_animation_cron(self):
        if not OSCAR_APP_URL or not OSCAR_PASSWORD:
            return
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f'{OSCAR_APP_URL}/api/cron-animation',
                headers={'x-password': OSCAR_PASSWORD},
            ) as res:
                logger.info(f'Oscar animation cron: {res.status}')

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get('custom_id', '')

        if custom_id.startswith('oscar_post:'):
            item_id = custom_id.split(':', 1)[1]
            await interaction.response.defer(ephemeral=True)
            await self._handle_post(interaction, item_id)
        elif custom_id.startswith('oscar_regen_caption:'):
            item_id = custom_id.split(':', 1)[1]
            await interaction.response.defer(ephemeral=True)
            await self._handle_regen_caption(interaction, item_id)
        elif custom_id.startswith('oscar_post_reel:'):
            item_id = custom_id.split(':', 1)[1]
            await interaction.response.defer(ephemeral=True)
            await self._handle_post_reel(interaction, item_id)

    async def _handle_post(self, interaction: discord.Interaction, item_id: str):
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f'{SUPABASE_URL}/rest/v1/oscar_queue?id=eq.{item_id}&select=*',
                headers={
                    'apikey': SUPABASE_KEY,
                    'Authorization': f'Bearer {SUPABASE_KEY}',
                },
            ) as res:
                data = await res.json()

            if not data:
                await interaction.followup.send('Polozka nenajdena.', ephemeral=True)
                return

            item = data[0]

            if item.get('status') == 'posted':
                await interaction.followup.send('Uz postnuté.', ephemeral=True)
                return

            photo_url = item.get('photo_url', '')
            caption = item.get('caption', '')

            if not photo_url:
                await interaction.followup.send('Ziadna fotka.', ephemeral=True)
                return

            # Step 1: Create Instagram media container
            async with session.post(
                f'https://graph.facebook.com/v21.0/{IG_USER_ID}/media',
                params={
                    'image_url': photo_url,
                    'caption': caption,
                    'access_token': IG_ACCESS_TOKEN,
                },
            ) as res:
                ig_data = await res.json()

            if 'error' in ig_data:
                await interaction.followup.send(f'IG chyba: {ig_data["error"]["message"]}', ephemeral=True)
                return

            container_id = ig_data['id']

            # Wait for container to be ready
            import asyncio
            for _ in range(15):
                await asyncio.sleep(6)
                async with session.get(
                    f'https://graph.facebook.com/v21.0/{container_id}',
                    params={'fields': 'status_code', 'access_token': IG_ACCESS_TOKEN},
                ) as res:
                    sc_data = await res.json()
                if sc_data.get('status_code') == 'FINISHED':
                    break
                if sc_data.get('status_code') == 'ERROR':
                    await interaction.followup.send('IG container chyba.', ephemeral=True)
                    return

            # Step 2: Publish
            async with session.post(
                f'https://graph.facebook.com/v21.0/{IG_USER_ID}/media_publish',
                params={
                    'creation_id': container_id,
                    'access_token': IG_ACCESS_TOKEN,
                },
            ) as res:
                publish_data = await res.json()

            if 'error' in publish_data:
                await interaction.followup.send(f'IG publish chyba: {publish_data["error"]["message"]}', ephemeral=True)
                return

            # Update status
            async with session.patch(
                f'{SUPABASE_URL}/rest/v1/oscar_queue?id=eq.{item_id}',
                headers={
                    'apikey': SUPABASE_KEY,
                    'Authorization': f'Bearer {SUPABASE_KEY}',
                    'Content-Type': 'application/json',
                    'Prefer': 'return=minimal',
                },
                data=json.dumps({'status': 'posted'}),
            ):
                pass

            # Clean up storage files after posting
            for url in [item.get('photo_url'), item.get('generated_url')]:
                await _delete_storage_file(session, url)

            await interaction.followup.send('Postnuté na Instagram!', ephemeral=True)

    async def _handle_regen_caption(self, interaction: discord.Interaction, item_id: str):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f'{OSCAR_APP_URL}/api/regenerate-caption',
                headers={'x-secret': INTERNAL_SECRET, 'Content-Type': 'application/json'},
                data=json.dumps({'itemId': item_id}),
            ) as res:
                if res.status == 200:
                    await interaction.followup.send('Novy popis priradeny.', ephemeral=True)
                else:
                    text = await res.text()
                    await interaction.followup.send(f'Chyba: {text}', ephemeral=True)

    async def _handle_post_reel(self, interaction: discord.Interaction, item_id: str):
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f'{SUPABASE_URL}/rest/v1/oscar_queue?id=eq.{item_id}&select=*',
                headers={'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}'},
            ) as res:
                data = await res.json()

            if not data:
                await interaction.followup.send('Polozka nenajdena.', ephemeral=True)
                return

            item = data[0]
            if item.get('status') == 'posted':
                await interaction.followup.send('Uz postnuté.', ephemeral=True)
                return

            video_url = item.get('photo_url', '')
            caption = item.get('caption', '')

            # Create Reel container
            async with session.post(
                f'https://graph.facebook.com/v21.0/{IG_USER_ID}/media',
                params={
                    'media_type': 'REELS',
                    'video_url': video_url,
                    'caption': caption,
                    'access_token': IG_ACCESS_TOKEN,
                },
            ) as res:
                ig_data = await res.json()

            if 'error' in ig_data:
                await interaction.followup.send(f'IG chyba: {ig_data["error"]["message"]}', ephemeral=True)
                return

            container_id = ig_data['id']
            await interaction.followup.send('Spracovavam video... chvilu potrva.', ephemeral=True)

            # Poll until ready
            import asyncio
            for _ in range(20):
                await asyncio.sleep(10)
                async with session.get(
                    f'https://graph.facebook.com/v21.0/{container_id}',
                    params={'fields': 'status_code', 'access_token': IG_ACCESS_TOKEN},
                ) as res:
                    status_data = await res.json()
                if status_data.get('status_code') == 'FINISHED':
                    break
                if status_data.get('status_code') == 'ERROR':
                    await interaction.followup.send('IG video spracovanie zlyhalo.', ephemeral=True)
                    return

            # Publish
            async with session.post(
                f'https://graph.facebook.com/v21.0/{IG_USER_ID}/media_publish',
                params={'creation_id': container_id, 'access_token': IG_ACCESS_TOKEN},
            ) as res:
                publish_data = await res.json()

            if 'error' in publish_data:
                await interaction.followup.send(f'IG publish chyba: {publish_data["error"]["message"]}', ephemeral=True)
                return

            async with session.patch(
                f'{SUPABASE_URL}/rest/v1/oscar_queue?id=eq.{item_id}',
                headers={'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}',
                         'Content-Type': 'application/json', 'Prefer': 'return=minimal'},
                data=json.dumps({'status': 'posted'}),
            ):
                pass

            # Clean up storage files after posting
            for url in [item.get('photo_url'), item.get('generated_url')]:
                await _delete_storage_file(session, url)

            await interaction.followup.send('Reel postnuty na Instagram!', ephemeral=True)


async def setup(bot):
    await bot.add_cog(Oscar(bot))
