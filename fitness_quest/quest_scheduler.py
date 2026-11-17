"""Publish organizer-authored quests on four secret random mini-quest days per week."""
import datetime as dt
import logging
import os
import random
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks

log = logging.getLogger('fitness-quest.calendar')
TZ = ZoneInfo('America/New_York')


def season_start():
    raw = os.getenv('FITNESS_QUEST_SEASON_START', '').strip()
    if not raw:
        return None
    start = dt.date.fromisoformat(raw)
    if start.weekday() != 0:
        raise ValueError('FITNESS_QUEST_SEASON_START must be a Monday')
    return start


def quest_open(q, now=None):
    """Use the same ET boundaries for the board, selector, and modal submission."""
    now = now or dt.datetime.now(TZ)
    return (q['published'] and q['release_date'] is not None
            and q['release_date'] <= now.date() <= q['due_date'])


async def publish_due(pool):
    """Prepare a complete week once; release only quests due as of today."""
    start = season_start()
    if start is None:
        return 0
    today = dt.datetime.now(TZ).date()
    week = today - dt.timedelta(days=today.weekday())
    if not start <= week < start + dt.timedelta(weeks=12):
        return 0
    async with pool.acquire() as con:
        async with con.transaction():
            # Prevent competing bot instances from assigning different random days.
            await con.execute('SELECT pg_advisory_xact_lock($1)', 20261130)
            rows = await con.fetch('''SELECT id,kind,release_date,due_date,published FROM quests
                WHERE week_start=$1 ORDER BY id FOR UPDATE''', week)
            minis = [r for r in rows if r['kind'] == 'mini']
            mains = [r for r in rows if r['kind'] == 'main']
            if len(minis) != 4 or len(mains) != 1:
                log.warning('Week %s incomplete: %s mini, %s main; no quests released', week, len(minis), len(mains))
                return 0
            scheduled = [r for r in rows if r['release_date'] is not None]
            if not scheduled:
                # The selected days are secret, persisted, and never rerolled.
                days = random.SystemRandom().sample(range(7), 4)
                for q, offset in zip(minis, days):
                    day = week + dt.timedelta(days=offset)
                    await con.execute('UPDATE quests SET release_date=$2,due_date=$2 WHERE id=$1', q['id'], day)
                await con.execute('UPDATE quests SET release_date=$2,due_date=$3 WHERE id=$1',
                                  mains[0]['id'], week, week + dt.timedelta(days=6))
                log.info('Persisted random mini quest dates for week %s', week)
            elif len(scheduled) != 5:
                log.error('Partial schedule for %s: %s/5 quests have dates; refusing to publish', week, len(scheduled))
                return 0
            result = await con.execute('''UPDATE quests SET published=true
                WHERE week_start=$1 AND release_date <= $2 AND published=false''', week, today)
            count = int(result.split()[-1])
            if count:
                log.info('Released %s quests for week %s', count, week)
            return count


async def announce_due(bot, pool):
    """Retry unsent announcements. A stable marker also helps recover from a crash after send."""
    channel_id = int(os.getenv('WEEKLY_QUESTS_CHANNEL_ID') or '0')
    if not channel_id:
        log.warning('WEEKLY_QUESTS_CHANNEL_ID missing; quest announcements disabled')
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    today = dt.datetime.now(TZ).date()
    async with pool.acquire() as con:
        rows = await con.fetch('''SELECT id,week_start,kind,title,instructions,xp,proof_type,due_date
            FROM quests WHERE published=true AND release_date <= $1 AND due_date >= $1
              AND announcement_message_id IS NULL ORDER BY release_date,kind,id''', today)
    for q in rows:
        marker = f'Quest ID: {q["id"]}'
        # If the send succeeded but the DB update failed, find the existing message.
        existing = None
        async for msg in channel.history(limit=100):
            if msg.author.id == bot.user.id and any(marker in (e.footer.text or '') for e in msg.embeds):
                existing = msg
                break
        if existing is None:
            title = discord.utils.escape_mentions(discord.utils.escape_markdown(q['title']))
            instructions = discord.utils.escape_mentions(discord.utils.escape_markdown(q['instructions']))
            embed = discord.Embed(
                title=('⚔️ Main Quest' if q['kind'] == 'main' else '🎨 Daily Mini Quest') + f': {title}',
                description=instructions[:3500], color=0x8876c9)
            embed.add_field(name='Reward', value=f'{q["xp"]} XP')
            embed.add_field(name='Proof', value=q['proof_type'])
            embed.add_field(name='Deadline', value=f'{q["due_date"]} at 11:59 PM Eastern')
            embed.set_footer(text=marker)
            existing = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        async with pool.acquire() as con:
            await con.execute('''UPDATE quests SET announcement_message_id=$2
                WHERE id=$1 AND announcement_message_id IS NULL''', q['id'], existing.id)
        log.info('Announced quest %s in channel %s', q['id'], channel_id)


def register(bot, get_pool):
    @tasks.loop(minutes=5)
    async def calendar_loop():
        try:
            await publish_due(get_pool())
            await announce_due(bot, get_pool())
        except Exception:
            log.exception('Quest calendar publishing/announcement failed')

    @calendar_loop.before_loop
    async def before():
        await bot.wait_until_ready()

    return calendar_loop
