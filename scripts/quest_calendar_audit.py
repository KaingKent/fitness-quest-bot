"""Read-only audit: python quest_calendar_audit.py (requires existing .env)."""

# Load environment variables from the project root.
from pathlib import Path as _EnvPath
from dotenv import load_dotenv as _load_dotenv
_load_dotenv(_EnvPath(__file__).resolve().parent.parent / ".env")

import asyncio
import datetime as dt
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / '.env')


async def main():
    start = dt.date.fromisoformat(os.environ['FITNESS_QUEST_SEASON_START'])
    con = await asyncpg.connect(os.environ['DATABASE_URL'], ssl='require')
    try:
        print('Week  Start       Mini  Main  Scheduled  Published  Status')
        for i in range(12):
            day = start + dt.timedelta(weeks=i)
            rows = await con.fetch('''SELECT id,kind,title,release_date,due_date,published
                FROM quests WHERE week_start=$1 ORDER BY kind,id''', day)
            minis = [q for q in rows if q['kind']=='mini']
            mains = [q for q in rows if q['kind']=='main']
            scheduled = sum(q['release_date'] is not None for q in rows)
            published = sum(bool(q['published']) for q in rows)
            good = len(minis)==4 and len(mains)==1
            print(f'{i+1:>4}  {day}  {len(minis):>4}  {len(mains):>4}  {scheduled:>9}  {published:>9}  {"READY" if good else "INCOMPLETE"}')
            for q in rows:
                print(f'       #{q["id"]} {q["kind"]:4} {q["title"]} | release={q["release_date"]} due={q["due_date"]}')
    finally:
        await con.close()

asyncio.run(main())
