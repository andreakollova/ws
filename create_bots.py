#!/usr/bin/env python3
"""
Creates 25 fake bot profiles for Woeva social proof.
Uploads avatars to Supabase Storage and inserts profiles.

Usage:
    SUPABASE_URL=https://... SUPABASE_SERVICE_KEY=eyJ... python create_bots.py
"""

import os
import uuid
import json
import time
import requests

SUPABASE_URL = os.environ["SUPABASE_URL"]
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
}

AVATAR_DIR = "/Users/antik/Desktop/Projekty/Woeva materialy/avatar"

# 25 bots: name, gender, photo file (or None)
BOTS = [
    # Girls with photos
    {"name": "Zuzana",    "photo": "Dievcata/10.jpg"},
    {"name": "Katarína",  "photo": "Dievcata/11.jpg"},
    {"name": "Lucia",     "photo": "Dievcata/12.jpg"},
    {"name": "Monika",    "photo": "Dievcata/13.jpg"},
    {"name": "Jana",      "photo": "Dievcata/14.jpg"},
    {"name": "Petra",     "photo": "Dievcata/15.jpg"},
    {"name": "Veronika",  "photo": "Dievcata/16.jpg"},
    {"name": "Martina",   "photo": "Dievcata/17.jpg"},
    # Boys with photos
    {"name": "Marek",     "photo": "Chlapci/ 1.jpg"},
    {"name": "Tomáš",     "photo": "Chlapci/2.jpg"},
    {"name": "Lukáš",     "photo": "Chlapci/3.jpg"},
    {"name": "Michal",    "photo": "Chlapci/4.jpg"},
    {"name": "Jakub",     "photo": "Chlapci/5.jpg"},
    {"name": "Martin",    "photo": "Chlapci/6.jpg"},
    {"name": "Peter",     "photo": "Chlapci/8.jpg"},
    {"name": "Juraj",     "photo": "Chlapci/9.jpg"},
    # Without photos
    {"name": "Zuzka",     "photo": None},
    {"name": "Karolína",  "photo": None},
    {"name": "Nina",      "photo": None},
    {"name": "Barbora",   "photo": None},
    {"name": "Denisa",    "photo": None},
    {"name": "Matej",     "photo": None},
    {"name": "Dávid",     "photo": None},
    {"name": "Adam",      "photo": None},
    {"name": "Rastislav", "photo": None},
]


def upload_avatar(photo_rel_path: str, bot_id: str) -> str | None:
    file_path = os.path.join(AVATAR_DIR, photo_rel_path)
    if not os.path.exists(file_path):
        print(f"  WARNING: file not found: {file_path}")
        return None

    storage_path = f"bots/{bot_id}.jpg"
    with open(file_path, "rb") as f:
        data = f.read()

    res = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/avatars/{storage_path}",
        headers={**HEADERS, "Content-Type": "image/jpeg", "x-upsert": "true"},
        data=data,
    )
    if res.status_code in (200, 201):
        return f"{SUPABASE_URL}/storage/v1/object/public/avatars/{storage_path}"
    else:
        print(f"  Upload failed ({res.status_code}): {res.text}")
        return None


def create_auth_user(bot_id: str, email: str) -> bool:
    res = requests.post(
        f"{SUPABASE_URL}/auth/v1/admin/users",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={
            "id": bot_id,
            "email": email,
            "password": uuid.uuid4().hex + uuid.uuid4().hex,
            "email_confirm": True,
        },
    )
    if res.status_code in (200, 201):
        return True
    print(f"  Auth user create failed ({res.status_code}): {res.text}")
    return False


def create_profile(bot_id: str, name: str, avatar_url: str | None):
    res = requests.post(
        f"{SUPABASE_URL}/rest/v1/profiles",
        headers={**HEADERS, "Content-Type": "application/json", "Prefer": "return=minimal"},
        json={
            "id": bot_id,
            "name": name,
            "avatar_url": avatar_url,
        },
    )
    if res.status_code not in (200, 201):
        print(f"  Profile insert failed ({res.status_code}): {res.text}")


def main():
    bot_ids = []
    print(f"Creating {len(BOTS)} bot profiles...\n")

    for i, bot in enumerate(BOTS):
        bot_id = str(uuid.uuid4())
        email = f"bot_{bot_id[:8]}@woeva.internal"
        print(f"[{i+1}/{len(BOTS)}] {bot['name']} (id={bot_id[:8]}...)")

        # Upload avatar
        avatar_url = None
        if bot["photo"]:
            print(f"  Uploading {bot['photo']}...")
            avatar_url = upload_avatar(bot["photo"], bot_id)
            if avatar_url:
                print(f"  Avatar: {avatar_url}")

        # Create auth user
        if not create_auth_user(bot_id, email):
            continue

        # Create profile
        create_profile(bot_id, bot["name"], avatar_url)
        bot_ids.append(bot_id)
        print(f"  Done.")
        time.sleep(0.3)

    # Save IDs for bot use
    output = {"bot_ids": bot_ids}
    with open("bot_ids.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n✅ Created {len(bot_ids)} bots.")
    print(f"   IDs saved to bot_ids.json")
    print(f"\nAdd to Render env: BOT_FAKE_USER_IDS={','.join(bot_ids)}")


if __name__ == "__main__":
    main()
