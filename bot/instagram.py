import asyncio
import logging
import uuid

import httpx

from config import INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_ACCOUNT_ID, SUPABASE_URL, SUPABASE_KEY

GRAPH_BASE = "https://graph.facebook.com/v21.0"
STORAGE_BUCKET = "woeva-instagram"
logger = logging.getLogger(__name__)


async def post_to_instagram(image_bytes: bytes, caption: str) -> str:
    """Upload image and publish to Instagram. Returns published media_id."""
    if not INSTAGRAM_ACCESS_TOKEN or not INSTAGRAM_ACCOUNT_ID:
        raise ValueError("Instagram credentials not configured (INSTAGRAM_ACCESS_TOKEN / INSTAGRAM_ACCOUNT_ID)")

    async with httpx.AsyncClient(timeout=60) as client:
        # 1. Upload to Supabase Storage to get public URL
        public_url = await _upload_to_supabase(image_bytes)
        logger.info(f"Image uploaded: {public_url}")

        # 2. Create media container
        container_resp = await client.post(
            f"{GRAPH_BASE}/{INSTAGRAM_ACCOUNT_ID}/media",
            params={
                "image_url": public_url,
                "caption": caption,
                "access_token": INSTAGRAM_ACCESS_TOKEN,
            },
        )
        if container_resp.status_code >= 400:
            raise RuntimeError(f"Instagram media error {container_resp.status_code}: {container_resp.text}")
        container_id = container_resp.json()["id"]
        logger.info(f"Created container: {container_id}")

        # 3. Wait for container to be ready
        await _wait_for_container(client, container_id)

        # 4. Publish
        publish_resp = await client.post(
            f"{GRAPH_BASE}/{INSTAGRAM_ACCOUNT_ID}/media_publish",
            params={
                "creation_id": container_id,
                "access_token": INSTAGRAM_ACCESS_TOKEN,
            },
        )
        publish_resp.raise_for_status()
        media_id = publish_resp.json()["id"]
        logger.info(f"Published to Instagram: {media_id}")
        return media_id


async def _upload_to_supabase(image_bytes: bytes) -> str:
    from supabase import create_client
    db = create_client(SUPABASE_URL, SUPABASE_KEY)
    filename = f"{uuid.uuid4().hex}.jpg"

    def _do_upload():
        db.storage.from_(STORAGE_BUCKET).upload(
            path=filename,
            file=image_bytes,
            file_options={"content-type": "image/jpeg"},
        )
        return db.storage.from_(STORAGE_BUCKET).get_public_url(filename)

    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(None, _do_upload),
        timeout=60,
    )


async def _wait_for_container(
    client: httpx.AsyncClient,
    container_id: str,
    max_tries: int = 10,
):
    for attempt in range(1, max_tries + 1):
        await asyncio.sleep(6)
        resp = await client.get(
            f"{GRAPH_BASE}/{container_id}",
            params={"fields": "status_code", "access_token": INSTAGRAM_ACCESS_TOKEN},
        )
        resp.raise_for_status()
        status_code = resp.json().get("status_code", "")
        logger.debug(f"Container {container_id} status ({attempt}/{max_tries}): {status_code}")
        if status_code == "FINISHED":
            return
        if status_code == "ERROR":
            raise RuntimeError(f"Instagram container ERROR for {container_id}")
    raise RuntimeError(f"Instagram container {container_id} did not reach FINISHED after {max_tries} attempts")
