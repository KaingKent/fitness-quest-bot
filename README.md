# Fitness Quest Bot — Phase 1 Prototype

A starter implementation of the Discord workout flow. **Not production ready.**

## Features included
- Persistent Discord dashboard buttons (`/setup_hub`)
- Private workout modal (type, duration, date, notes)
- Optional image via `/workout_image` (Discord attachment URL only)
- Opt-in public workout posting
- Supabase Postgres schema, weekly normalized fitness XP
- Background Google Sheets `Workout Logs` sync with retry

## Not implemented yet
- Supabase Storage image archival and private image viewing
- Image attachment directly through the button flow (currently `/workout_image` requires `/my_workouts` ID)
- Player registration and admin-approved weekly plans (default 3 workouts)
- Eligibility checks beyond date/length and duplicates; correction window enforcement
- Workout editing/deletion and XP ledger
- Challenges, reflections, milestones, achievements, levels, leaderboard
- Season dates and weekly schedule
- Hardened outbox reconciliation, security reviews and integration tests

## Setup
1. Create a Discord application and bot in the Discord Developer Portal. Invite it with `bot` and `applications.commands` scopes and Send Messages, Embed Links permissions. No Message Content privileged intent is needed.
2. Create a Supabase project. In SQL Editor, run `schema.sql`. Obtain a PostgreSQL connection URI that is reachable from EC2 (Supabase session pooler may be appropriate for IPv4-only instances).
3. On EC2 install Python 3.11+; create a virtual environment, `pip install -r requirements.txt`.
4. Copy `.env.example` to `.env`, fill in Discord token, server (guild) ID, and database URI. Keep `.env` private (`chmod 600 .env`).
5. Optional Sheets: Create a Google Cloud service account, enable Google Sheets API, download JSON credentials to `service-account.json`, share a spreadsheet with the service-account email as Editor, and set `GOOGLE_SHEET_ID`.
6. Run `python bot.py`. In your Discord server run `/setup_hub` as an admin.
7. Test workout form, `/my_workouts`, optional `/workout_image`, and public share button.

## Caveats
- The default weekly target of 3 is **test data**, not the final approved plan workflow.
- `image_discord_url` is a temporary Discord-hosted attachment URL; permanent Supabase Storage handling is the next implementation task.
- Public share happens immediately if a workout channel is configured; image updates do not edit the public message yet.
- The sync worker currently handles new records; for edits, the outbox must be requeued. The schema's unique constraint prevents a second pending upsert event after the first is processed, so edit sync must be improved before production.
- Set `WORKOUT_CHANNEL_ID` to a channel in your Discord server for public sharing.
- Google Sheets is an admin view: do not share it publicly if it contains notes, image URLs, or user identifiers.
