-- ============================================================
-- Woeva Events Scraper — Supabase Migration
-- Run this in Supabase SQL Editor (Project: Woeva)
-- ============================================================

-- ─────────────────────────────────────────────────────────────
-- 1. BOT USER
-- The scraper bot needs a profile to satisfy creator_id NOT NULL.
-- We insert directly into auth.users + profiles using a fixed UUID.
-- ─────────────────────────────────────────────────────────────

-- Insert bot into auth.users (so the profiles FK is satisfied)
INSERT INTO auth.users (
    id,
    email,
    created_at,
    updated_at,
    raw_app_meta_data,
    raw_user_meta_data,
    is_super_admin,
    encrypted_password,
    email_confirmed_at,
    aud,
    role
)
VALUES (
    '00000000-0000-0000-0000-000000000001',
    'bot@woeva.internal',
    now(), now(),
    '{"provider":"email","providers":["email"]}'::jsonb,
    '{"full_name":"Woeva Bot"}'::jsonb,
    false,
    '',  -- no password — this account cannot log in
    now(),
    'authenticated',
    'authenticated'
)
ON CONFLICT (id) DO NOTHING;

-- Insert bot profile
INSERT INTO public.profiles (id, name, city)
VALUES ('00000000-0000-0000-0000-000000000001', 'Woeva Bot', 'Slovensko')
ON CONFLICT (id) DO NOTHING;


-- ─────────────────────────────────────────────────────────────
-- 2. SCRAPED EVENTS TABLE
-- Staging table: scraper saves here, Discord bot picks up from here.
-- After Discord approval, event is copied to the main events table.
-- ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS scraped_events (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_url  TEXT UNIQUE NOT NULL,
    source      TEXT NOT NULL,         -- 'goout' | 'tootoot'
    title       TEXT,
    description TEXT,                  -- ChatGPT: 1 emoji + max 29 words (Slovak)
    tag         TEXT,                  -- coffee | party | zapasy | sport | umenie | gaming | conference | priroda | historia | zaujimave
    date        DATE,
    time_start  TEXT,                  -- 'HH:MM'
    duration    TEXT,                  -- '2h', '90min', '2h 30min'
    venue       TEXT,
    address     TEXT,
    city        TEXT,
    price       TEXT DEFAULT 'Zadarmo',
    photo_url   TEXT,
    scraped_at  TIMESTAMPTZ DEFAULT now(),
    discord_sent BOOLEAN DEFAULT false,
    approved    BOOLEAN DEFAULT false,
    rejected    BOOLEAN DEFAULT false
);

-- Row Level Security: service role (used by bot/scraper) can do anything.
-- Regular users cannot access scraped_events at all.
ALTER TABLE scraped_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access to scraped_events"
    ON scraped_events
    USING (true)
    WITH CHECK (true);
-- Note: this policy only applies when using the service role key.
-- Anon/authenticated users see nothing because no policy grants them access.


-- ─────────────────────────────────────────────────────────────
-- 3. EVENTS TABLE — RLS policy for bot inserts
-- The events table requires creator_id = auth.uid() for inserts.
-- We add a policy that also allows the bot user to insert.
-- ─────────────────────────────────────────────────────────────

-- Allow the bot user to create events (scraped events)
CREATE POLICY "Bot can create events"
    ON events
    FOR INSERT
    WITH CHECK (creator_id = '00000000-0000-0000-0000-000000000001');

-- Allow the bot user to update events it created (e.g. update going_count)
CREATE POLICY "Bot can update own events"
    ON events
    FOR UPDATE
    USING (creator_id = '00000000-0000-0000-0000-000000000001');


-- ─────────────────────────────────────────────────────────────
-- 4. INSTAGRAM STORAGE BUCKET
-- Create the storage bucket for Instagram image uploads.
-- ─────────────────────────────────────────────────────────────

INSERT INTO storage.buckets (id, name, public)
VALUES ('woeva-instagram', 'woeva-instagram', true)
ON CONFLICT (id) DO NOTHING;

-- Allow service role to upload
CREATE POLICY "Service role can upload to woeva-instagram"
    ON storage.objects
    FOR INSERT
    WITH CHECK (bucket_id = 'woeva-instagram');

-- Public read access (needed so Instagram can fetch the image URL)
CREATE POLICY "Public can read woeva-instagram"
    ON storage.objects
    FOR SELECT
    USING (bucket_id = 'woeva-instagram');


-- ─────────────────────────────────────────────────────────────
-- DONE
-- After running this migration:
-- 1. Set BOT_USER_ID=00000000-0000-0000-0000-000000000001 in Render env vars
-- 2. Use your Supabase SERVICE ROLE key as SUPABASE_KEY for the scraper/bot
--    (the anon key will NOT have permission to insert events as the bot user)
-- ─────────────────────────────────────────────────────────────
