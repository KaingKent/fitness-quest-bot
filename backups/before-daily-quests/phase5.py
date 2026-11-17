"""Publish organizer-prepared quests automatically during their scheduled week."""
import datetime as dt
import logging
import os
from zoneinfo import ZoneInfo
from discord.ext import tasks

log=logging.getLogger('fitness-quest.calendar')
TZ=ZoneInfo('America/New_York')

async def publish_due(pool):
    raw=os.getenv('FITNESS_QUEST_SEASON_START','').strip()
    if not raw:
        return 0
    start=dt.date.fromisoformat(raw)
    if start.weekday()!=0:
        raise ValueError('FITNESS_QUEST_SEASON_START must be a Monday')
    today=dt.datetime.now(TZ).date()
    week=today-dt.timedelta(days=today.weekday())
    if not start<=week<start+dt.timedelta(weeks=12):
        return 0
    async with pool.acquire() as con:
        result=await con.execute('UPDATE quests SET published=true WHERE week_start=$1 AND published=false',week)
    count=int(result.split()[-1]);
    if count: log.info('Published %s scheduled quests for %s',count,week)
    return count

def register(bot,get_pool):
    @tasks.loop(minutes=5)
    async def calendar_loop():
        try: await publish_due(get_pool())
        except Exception: log.exception('Quest calendar publishing failed')

    @calendar_loop.before_loop
    async def before(): await bot.wait_until_ready()
    return calendar_loop
