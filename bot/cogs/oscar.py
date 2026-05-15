import discord
from discord.ext import commands
import os
import aiohttp
import json

OSCAR_APP_URL = os.environ.get('OSCAR_APP_URL', '').rstrip('/')
INTERNAL_SECRET = os.environ.get('INTERNAL_SECRET', '')
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')
IG_USER_ID = os.environ.get('IG_USER_ID', '')
IG_ACCESS_TOKEN = os.environ.get('IG_ACCESS_TOKEN', '')


class Oscar(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get('custom_id', '')

        if custom_id.startswith('oscar_recreate:'):
            item_id = custom_id.split(':', 1)[1]
            await interaction.response.defer(ephemeral=True)
            await self._handle_recreate(interaction, item_id)
        elif custom_id.startswith('oscar_post:'):
            item_id = custom_id.split(':', 1)[1]
            await interaction.response.defer(ephemeral=True)
            await self._handle_post(interaction, item_id)

    async def _handle_recreate(self, interaction: discord.Interaction, item_id: str):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f'{OSCAR_APP_URL}/api/regenerate',
                headers={
                    'x-secret': INTERNAL_SECRET,
                    'Content-Type': 'application/json',
                },
                data=json.dumps({'itemId': item_id}),
            ) as res:
                if res.status == 200:
                    await interaction.followup.send('Regenerujem... bude to chvilku.', ephemeral=True)
                else:
                    text = await res.text()
                    await interaction.followup.send(f'Chyba: {text}', ephemeral=True)

    async def _handle_post(self, interaction: discord.Interaction, item_id: str):
        async with aiohttp.ClientSession() as session:
            # Fetch item from Supabase
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

            if not item.get('generated_url'):
                await interaction.followup.send('Ziadny vygenerovany obrazok.', ephemeral=True)
                return

            if item.get('status') == 'posted':
                await interaction.followup.send('Uz postnuté.', ephemeral=True)
                return

            caption = item.get('caption', '')

            # Step 1: Create Instagram media container
            async with session.post(
                f'https://graph.facebook.com/v21.0/{IG_USER_ID}/media',
                params={
                    'image_url': item['generated_url'],
                    'caption': caption,
                    'access_token': IG_ACCESS_TOKEN,
                },
            ) as res:
                ig_data = await res.json()

            if 'error' in ig_data:
                await interaction.followup.send(f'IG chyba: {ig_data["error"]["message"]}', ephemeral=True)
                return

            container_id = ig_data['id']

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

            # Update status to posted
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

            await interaction.followup.send('Postnuté na Instagram!', ephemeral=True)


async def setup(bot):
    await bot.add_cog(Oscar(bot))
