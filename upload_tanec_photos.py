#!/usr/bin/env python3
"""
Uploads tanec/fontana event photos to Supabase Storage.

Usage:
    SUPABASE_URL=https://... SUPABASE_SERVICE_KEY=eyJ... python upload_tanec_photos.py
"""

import os
import requests

SUPABASE_URL = os.environ["SUPABASE_URL"]
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
}

PHOTO_DIR = "/Users/antik/Desktop/Projekty/Woeva materialy/tanec"

def upload_photo(filename: str) -> str | None:
    file_path = os.path.join(PHOTO_DIR, filename)
    storage_path = f"tanec/{filename}"
    with open(file_path, "rb") as f:
        data = f.read()

    content_type = "image/webp"
    res = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/event-covers/{storage_path}",
        headers={**HEADERS, "Content-Type": content_type, "x-upsert": "true"},
        data=data,
    )
    if res.status_code in (200, 201):
        url = f"{SUPABASE_URL}/storage/v1/object/public/event-covers/{storage_path}"
        print(f"  Uploaded: {url}")
        return url
    else:
        print(f"  Upload failed ({res.status_code}): {res.text}")
        return None


def main():
    photos = sorted(f for f in os.listdir(PHOTO_DIR) if f.endswith(".webp"))
    print(f"Uploading {len(photos)} photos...\n")

    urls = []
    for photo in photos:
        print(f"Uploading {photo}...")
        url = upload_photo(photo)
        if url:
            urls.append(url)

    print(f"\nDone. {len(urls)} photos uploaded.")
    print("\nAdd these to TANEC_PHOTO_URLS in eventland_scraper.py:")
    for url in urls:
        print(f'    "{url}",')


if __name__ == "__main__":
    main()
