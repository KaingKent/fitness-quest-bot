"""Single-guild instance bootstrap. Each process uses its own Discord app and database."""
import asyncio
import datetime as dt
import logging
import os
from pathlib import Path

import discord
from dotenv import dotenv_values

from fitness_quest import level_roles

log = logging.getLogger('fitness-quest.server_setup')
ENV_KEYS = {
    'APPROVAL_CHANNEL_ID': 'plan-approvals',
    'JOURNAL_CATEGORY_ID': 'ADVENTURER JOURNALS',
    'QUEST_SUBMISSIONS_CHANNEL_ID': 'quest-submissions',
    'ACHIEVEMENTS_CHANNEL_ID': 'achievements',
    'WEEKLY_QUESTS_CHANNEL_ID': 'weekly-quests',
    'PRIVATE_QUEST_SUBMISSIONS_CHANNEL_ID': 'private-quest-submissions',
}


def _write_env(path: Path, updates: dict[str, str]):
    """Update only managed keys; back up original and keep secret file private."""
    old = path.read_text(encoding='utf-8')
    lines = old.splitlines()
    replaced = set()
    result = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split('=', 1)[0].strip() if '=' in stripped and not stripped.startswith('#') else ''
        if key in updates:
            if key not in replaced:
                result.append(f'{key}={updates[key]}')
                replaced.add(key)
        else:
            result.append(line)
    for key, value in updates.items():
        if key not in replaced:
            result.append(f'{key}={value}')
    backup = path.with_name(path.name + '.before-setup-server')
    if not backup.exists():
        backup.write_text(old, encoding='utf-8')
        backup.chmod(0o600)
    temporary = path.with_name(path.name + '.setup-tmp')
    temporary.write_text('\n'.join(result).rstrip() + '\n', encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)


async def setup(interaction: discord.Interaction, bot, pool, hub_view_factory, hub_text: str, env_path: Path, start_date: str):
    guild = interaction.guild
    if guild is None or guild.id != int(os.environ['DISCORD_GUILD_ID']):
        return await interaction.response.send_message('Use this in the server configured for this bot instance.', ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message('Server Administrator permission is required.', ephemeral=True)
    try:
        start = dt.date.fromisoformat(start_date)
    except ValueError:
        return await interaction.response.send_message('Enter the start date as YYYY-MM-DD.', ephemeral=True)
    if start.weekday() != 0:
        return await interaction.response.send_message('The 12-week season must start on a Monday.', ephemeral=True)
    if start < dt.datetime.now(dt.timezone.utc).date():
        return await interaction.response.send_message('Choose today or a future Monday.', ephemeral=True)
    me = guild.me
    if not me or not (me.guild_permissions.manage_channels and me.guild_permissions.manage_roles and me.guild_permissions.send_messages):
        return await interaction.response.send_message('Bot requires Manage Channels, Manage Roles, and Send Messages permissions.', ephemeral=True)
    if me.top_role <= guild.default_role:
        return await interaction.response.send_message('Move the bot role above @everyone in Server Settings → Roles.', ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        public_category = discord.utils.get(guild.categories, name='THE CHUNGUS QUEST')
        if public_category is None:
            public_category = await guild.create_category('THE CHUNGUS QUEST', reason='Chungus Quest setup')
        private_category = discord.utils.get(guild.categories, name='QUEST ADMINISTRATION')
        if private_category is None:
            private_category = await guild.create_category('QUEST ADMINISTRATION', reason='Chungus Quest setup')
        journal_category = discord.utils.get(guild.categories, name='ADVENTURER JOURNALS')
        if journal_category is None:
            journal_category = await guild.create_category('ADVENTURER JOURNALS', reason='Chungus Quest setup')

        bot_overwrites = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, embed_links=True, manage_messages=True)
        private_overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False), me: bot_overwrites}
        async def channel(name, category, private=False):
            # Reuse channels in the intended category, rather than duplicating them.
            existing = discord.utils.get(category.text_channels, name=name)
            if existing is not None:
                return existing
            return await guild.create_text_channel(name, category=category,
                overwrites=private_overwrites if private else None, reason='Chungus Quest setup')

        hub = await channel('chungus-hub', public_category)
        weekly = await channel('weekly-quests', public_category)
        submissions = await channel('quest-submissions', public_category)
        achievements = await channel('achievements', public_category)
        approvals = await channel('plan-approvals', private_category, private=True)
        private_submissions = await channel('private-quest-submissions', private_category, private=True)
        # If a reused channel was public, make the intended private channels private.
        for private_channel in (approvals, private_submissions):
            await private_channel.set_permissions(guild.default_role, view_channel=False, reason='Chungus Quest admin privacy')
            await private_channel.set_permissions(me, overwrite=bot_overwrites, reason='Chungus Quest bot access')
        await level_roles.ensure_roles(guild)

        async with pool.acquire() as con:
            await con.execute('''CREATE TABLE IF NOT EXISTS bot_hub_messages (
                guild_id bigint NOT NULL, channel_id bigint NOT NULL,
                message_id bigint NOT NULL, PRIMARY KEY(guild_id,channel_id))''')
            existing_id = await con.fetchval('SELECT message_id FROM bot_hub_messages WHERE guild_id=$1 AND channel_id=$2', guild.id, hub.id)
        message = None
        if existing_id:
            try:
                message = await hub.fetch_message(existing_id)
            except discord.NotFound:
                pass
        if message is None:
            async for candidate in hub.history(limit=100):
                if candidate.author.id == bot.user.id and 'Begin your adventure' in candidate.content:
                    message = candidate
                    break
        if message:
            await message.edit(content=hub_text, view=hub_view_factory())
        else:
            message = await hub.send(hub_text, view=hub_view_factory())
        async with pool.acquire() as con:
            await con.execute('''INSERT INTO bot_hub_messages(guild_id,channel_id,message_id)
                VALUES($1,$2,$3) ON CONFLICT(guild_id,channel_id)
                DO UPDATE SET message_id=excluded.message_id''', guild.id, hub.id, message.id)

        updates = {
            'DISCORD_GUILD_ID': str(guild.id),
            'FITNESS_QUEST_SEASON_START': start.isoformat(),
            'APPROVAL_CHANNEL_ID': str(approvals.id),
            'JOURNAL_CATEGORY_ID': str(journal_category.id),
            'QUEST_SUBMISSIONS_CHANNEL_ID': str(submissions.id),
            'ACHIEVEMENTS_CHANNEL_ID': str(achievements.id),
            'WEEKLY_QUESTS_CHANNEL_ID': str(weekly.id),
            'PRIVATE_QUEST_SUBMISSIONS_CHANNEL_ID': str(private_submissions.id),
        }
        if not env_path.is_file():
            raise RuntimeError(f'Missing .env file at {env_path}')
        _write_env(env_path, updates)
        await interaction.followup.send(
            f'**Server setup complete.** Created/reused channels, 15 collectible roles, and dashboard in {hub.mention}. '
            f'Season begins **{start.isoformat()}** (midnight Eastern). '
            '**Restart this bot instance** to load its saved channel IDs and start date. '
            'Private admin channels are visible to members with Discord Administrator permission; review permissions before inviting moderators. '
            'Each instance must use its own Discord application/token and isolated database.', ephemeral=True)
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.exception('Discord server setup failed')
        await interaction.followup.send(f'Setup stopped due to Discord permissions or API error: `{type(exc).__name__}`. '
            'Any channels already created will be reused when you retry.', ephemeral=True)
    except Exception:
        log.exception('Server setup failed')
        await interaction.followup.send('Setup failed. Check the bot logs; rerunning setup reuses existing channels.', ephemeral=True)
