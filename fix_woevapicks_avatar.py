#!/usr/bin/env python3
"""
Re-uploads woevapicks.jpg to Supabase Storage and updates BOT_USER_ID profile.

Usage:
    SUPABASE_URL=https://... SUPABASE_SERVICE_KEY=eyJ... python fix_woevapicks_avatar.py
"""

import os
import requests

SUPABASE_URL = os.environ["SUPABASE_URL"]
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BOT_USER_ID = os.environ.get("BOT_USER_ID", "00000000-0000-0000-0000-000000000001")

HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
}

PHOTO_PATH = "/Users/antik/Desktop/Projekty/Woeva materialy/woevapicks.jpg"
STORAGE_PATH = "woevapicks.jpg"
BUCKET = "avatars"


def upload():
    with open(PHOTO_PATH, "rb") as f:
        data = f.read()

    res = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{STORAGE_PATH}",
        headers={**HEADERS, "Content-Type": "image/jpeg", "x-upsert": "true"},
        data=data,
    )
    if res.status_code in (200, 201):
        url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{STORAGE_PATH}"
        print(f"Uploaded: {url}")
        return url
    else:
        print(f"Upload failed ({res.status_code}): {res.text}")
        return None


def update_profile(avatar_url: str):
    res = requests.patch(
        f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{BOT_USER_ID}",
        headers={**HEADERS, "Content-Type": "application/json", "Prefer": "return=minimal"},
        json={"name": "Woeva Picks", "avatar_url": avatar_url},
    )
    if res.status_code in (200, 204):
        print(f"Profile updated for {BOT_USER_ID}")
    else:
        print(f"Profile update failed ({res.status_code}): {res.text}")


def main():
    url = upload()
    if url:
        update_profile(url)
        print("\nDone. Restart the app to see the change.")


if __name__ == "__main__":
    main()
