"""Collectible character-title roles and level-up announcements."""
import asyncio
import logging
import os
import discord

log = logging.getLogger('fitness-quest.level_roles')
TITLES = (
    'Chud', 'Big Back', 'Sand Head', 'Lemon', 'Monke',
    'Chopped', 'Pizza Rat', 'Barnacle Brain', 'Crustaceous Cheapskate',
    'Mediocre Clarinet Player', 'Weenie Hut Junior', 'Big Meaty Claws',
    'From Stupid Town', 'Goofy Goober', 'Big Chungus',
)
GUILD_ID = int(os.environ['DISCORD_GUILD_ID'])
CHANNEL_ID = int(os.getenv('ACHIEVEMENTS_CHANNEL_ID') or 0)
_user_locks = {}


def title_for_level(level):
    return TITLES[max(1, min(15, int(level))) - 1]


def _guild(bot):
    return bot.get_guild(GUILD_ID) if bot else None


async def initialize(pool):
    async with pool.acquire() as con:
        await con.execute("""CREATE TABLE IF NOT EXISTS level_role_announcements (
            discord_user_id BIGINT NOT NULL REFERENCES players(discord_user_id) ON DELETE CASCADE,
            level INTEGER NOT NULL CHECK (level BETWEEN 1 AND 15),
            announced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY(discord_user_id, level)
        )""")


async def ensure_roles(guild):
    """Create cosmetic roles; reuse matching roles already present."""
    roles = {}
    for name in TITLES:
        role = discord.utils.get(guild.roles, name=name)
        if role is None:
            role = await guild.create_role(name=name, permissions=discord.Permissions.none(),
                                           mentionable=False, reason='Chungus Quest collectible title')
        roles[name] = role
    return roles


async def sync_player(bot, pool, uid, *, announce=True):
    """Award all earned titles and announce newly reached levels exactly once."""
    guild = _guild(bot)
    if guild is None:
        return
    lock = _user_locks.setdefault(uid, asyncio.Lock())
    async with lock:
        async with pool.acquire() as con:
            exists = await con.fetchval('SELECT 1 FROM players WHERE discord_user_id=$1 AND challenger_name IS NOT NULL', uid)
        if not exists:
            return
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        from fitness_quest import achievements, workouts
        xp = await achievements.total_xp(pool, uid)
        level, _ = workouts.level_and_title(xp)
        role_map = await ensure_roles(guild)
        missing = [role_map[TITLES[i]] for i in range(level) if role_map[TITLES[i]] not in member.roles]
        if missing:
            try:
                await member.add_roles(*missing, reason=f'Chungus Quest collectible titles through level {level}')
            except (discord.Forbidden, discord.HTTPException):
                log.exception('Could not grant title roles to %s. Check Manage Roles and hierarchy.', uid)
        # Level 1 is the starting title, not a level-up announcement.
        async with pool.acquire() as con:
            await con.execute("""INSERT INTO level_role_announcements(discord_user_id, level)
                VALUES($1,1) ON CONFLICT DO NOTHING""", uid)
            previously = {r['level'] for r in await con.fetch(
                'SELECT level FROM level_role_announcements WHERE discord_user_id=$1', uid)}
        if not announce:
            # Startup reconciliation is intentionally silent (no historical announcement spam).
            async with pool.acquire() as con:
                await con.executemany("""INSERT INTO level_role_announcements(discord_user_id,level)
                    VALUES($1,$2) ON CONFLICT DO NOTHING""", [(uid, n) for n in range(2, level + 1)])
            return
        channel = guild.get_channel(CHANNEL_ID) if CHANNEL_ID else None
        if channel is None and CHANNEL_ID:
            try:
                channel = await bot.fetch_channel(CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.warning('Achievements announcement channel %s unavailable', CHANNEL_ID)
        for new_level in range(2, level + 1):
            if new_level in previously:
                continue
            if channel is None:
                log.warning('Cannot announce level %s for %s: ACHIEVEMENTS_CHANNEL_ID missing', new_level, uid)
                break
            embed = discord.Embed(
                title='LEVEL UP!',
                description=(f'{member.mention} reached **Level {new_level}!**\n'
                             f'New title unlocked: **{title_for_level(new_level)}**\n'
                             'Another title added to the collection.'),
                color=discord.Color.gold())
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False))
            except (discord.Forbidden, discord.HTTPException):
                log.exception('Level announcement failed for %s at level %s', uid, new_level)
                break
            async with pool.acquire() as con:
                await con.execute("""INSERT INTO level_role_announcements(discord_user_id,level)
                    VALUES($1,$2) ON CONFLICT DO NOTHING""", uid, new_level)


async def reconcile(bot, pool):
    guild = _guild(bot)
    if guild is None:
        log.warning('Configured Discord guild is unavailable for title-role sync')
        return
    await ensure_roles(guild)
    async with pool.acquire() as con:
        rows = await con.fetch('SELECT discord_user_id FROM players WHERE challenger_name IS NOT NULL')
    for row in rows:
        try:
            await sync_player(bot, pool, row['discord_user_id'], announce=False)
        except Exception:
            log.exception('Could not reconcile roles for %s', row['discord_user_id'])
