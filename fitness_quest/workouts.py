"""Phase 2: approved plans, workout XP, profiles, leaderboard, and journals."""
import asyncio
import datetime as dt
import io
import logging
import os
import re
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger('fitness-quest.workouts')
TZ = ZoneInfo('America/New_York')
GUILD_ID = int(os.environ['DISCORD_GUILD_ID'])
APPROVAL_CHANNEL_ID = int(os.getenv('APPROVAL_CHANNEL_ID') or '0')
JOURNAL_CATEGORY_ID = int(os.getenv('JOURNAL_CATEGORY_ID') or '0')


def today():
    return dt.datetime.now(TZ).date()


def monday(day):
    return day - dt.timedelta(days=day.weekday())


def level_and_title(xp):
    level = min(15, 1 + xp // 100)
    titles = [(15, 'Mythic'), (13, 'Legend'), (11, 'Champion'), (9, 'Elite'),
              (7, 'Warrior'), (5, 'Challenger'), (3, 'Adventurer'), (1, 'Rookie')]
    return level, next(title for threshold, title in titles if level >= threshold)


def weekly_xp(count, target):
    if not target:
        return 0
    completed = min(count, target)
    # half-up rounding, unlike Python's round() bankers rounding
    progress = (60 * completed + target) // (2 * target)
    return progress + (20 if completed == target else 0)


async def get_week(pool, user_id, week_start):
    async with pool.acquire() as con:
        target = await con.fetchval('''SELECT weekly_target FROM plan_history
            WHERE discord_user_id=$1 AND effective_date <= $2
            ORDER BY effective_date DESC LIMIT 1''', user_id, week_start)
        count = await con.fetchval('''SELECT count(*) FROM workouts
            WHERE discord_user_id=$1 AND workout_date >= $2 AND workout_date < $3''',
            user_id, week_start, week_start + dt.timedelta(days=7))
    return int(count), target, weekly_xp(count, target)


async def workout_total(pool, user_id):
    async with pool.acquire() as con:
        rows = await con.fetch('''SELECT date_trunc('week', workout_date::timestamp)::date AS week_start,
            count(*)::integer AS completed FROM workouts WHERE discord_user_id=$1
            GROUP BY 1 ORDER BY 1''', user_id)
        plans = await con.fetch('''SELECT weekly_target, effective_date FROM plan_history
            WHERE discord_user_id=$1 ORDER BY effective_date''', user_id)
    total = 0
    for row in rows:
        active = [p for p in plans if p['effective_date'] <= row['week_start']]
        if active:
            total += weekly_xp(row['completed'], active[-1]['weekly_target'])
    return total


def font(size, bold=False):
    candidates = ['/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                  '/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf']
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _profile_font(size, *, heavy=False, japanese=False):
    """Load real fonts on Ubuntu; fail clearly instead of silently drawing tiny text.

    Install with: sudo apt-get install fonts-dejavu-core fonts-noto-cjk
    Place your own fortnite.otf inside the project's assets/ directory for the Fortnite-style headings.
    """
    from pathlib import Path
    import os
    from functools import lru_cache

    @lru_cache(maxsize=96)
    def load(size, heavy, japanese):
        base = Path(__file__).resolve().parent
        paths = []
        if japanese:
            paths += [
                '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
                '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
                '/usr/share/fonts/truetype/noto/NotoSansJP-Bold.ttf',
                '/usr/share/fonts/truetype/noto/NotoSansJP-Regular.ttf',
            ]
        elif heavy:
            paths += [
                str(Path(__file__).resolve().parent.parent / 'assets' / 'fortnite.otf'),
                str(base / 'profile_display.ttf'),
                '/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf',
                '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
                '/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf',
            ]
        else:
            paths += [
                '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf',
            ]
        for path in paths:
            try:
                return ImageFont.truetype(path, size)
            except (OSError, ValueError):
                continue
        # Pillow sometimes bundles DejaVu even when Ubuntu system fonts are absent.
        for family in (['DejaVuSans-Bold.ttf'] if heavy or japanese else ['DejaVuSans.ttf']):
            try:
                return ImageFont.truetype(family, size)
            except (OSError, ValueError):
                continue
        raise RuntimeError(
            'Profile fonts missing. On EC2 run: '
            'sudo apt-get update && sudo apt-get install -y fonts-dejavu-core fonts-noto-cjk'
        )
    return load(int(size), heavy, japanese)


def _fitted(draw, text, box, size, *, heavy=True, fill='#000000', japanese=False):
    x, y, max_width = box
    while size > 12:
        f = _profile_font(size, heavy=heavy, japanese=japanese)
        if draw.textbbox((0, 0), text, font=f)[2] <= max_width:
            break
        size -= 2
    draw.text((x, y), text, font=f, fill=fill, stroke_width=0)


def _steam_progress(draw, top, heading, done, total):
    left, right = 504, 1215
    _fitted(draw, heading, (left, top, 710), 43)
    panel_top = top + 54
    draw.rectangle((left, panel_top, right, panel_top + 82), fill='#333a45')
    done, total = max(0, int(done)), max(1, int(total))
    pct = min(100, round(done / total * 100))
    draw.text((left + 15, panel_top + 14), f"You've unlocked {done} of {total} ({pct}%)",
              font=_profile_font(17), fill='#e4e7ec')
    x1, x2, y1, y2 = left + 15, right - 15, panel_top + 52, panel_top + 67
    draw.rounded_rectangle((x1, y1, x2, y2), radius=8, fill='#05090e')
    if pct:
        draw.rounded_rectangle((x1, y1, x1 + (x2 - x1) * pct // 100, y2),
                               radius=8, fill='#358cf2')


def _draw_minecraft_xp_bar(canvas, rect, percent):
    """Minecraft-inspired HUD texture: each segment has a distinct green base."""
    import random
    x, y, width, height = rect
    percent = max(0, min(100, percent))
    scale = 3
    lw = (width + scale - 1) // scale
    lh = max(7, (height + scale - 1) // scale)
    tex = Image.new('RGB', (lw, lh), '#23351b')
    td = ImageDraw.Draw(tex)
    rng = random.Random(20261116)
    # The reference has distinct whole sections, not uniform green static.
    # Each segment owns a dominant shade plus large irregular pixel patches.
    bases = [(113, 155, 67), (161, 214, 57), (101, 149, 75),
             (119, 161, 78), (172, 218, 55), (103, 145, 72),
             (137, 184, 62), (94, 140, 68), (160, 203, 66),
             (111, 153, 79)]
    segments = len(bases)
    segment_w = lw / segments
    fill_limit = lw * percent / 100
    for sx in range(segments):
        start = round(sx * segment_w)
        stop = round((sx + 1) * segment_w)
        base = bases[sx]
        # Stable patch pattern per segment: broad 2-5 pixel blocks.
        patches = {}
        for cx in range(start, stop):
            for cy in range(1, lh - 1):
                if cx >= fill_limit:
                    shade = (28 + (sx % 3) * 3, 45 + (sx % 2) * 4, 25 + (sx % 3) * 2)
                else:
                    block = ((cx - start) // 3, cy // 2)
                    if block not in patches:
                        patches[block] = rng.choice([-30, -18, -8, 0, 7, 14, 24, 31])
                    v = patches[block]
                    if cy == 1:
                        v += 12
                    elif cy == lh - 2:
                        v -= 18
                    shade = tuple(max(0, min(255, channel + v)) for channel in base)
                td.point((cx, cy), fill=shade)
        if sx:
            td.line((start, 1, start, lh - 2), fill='#263a1b', width=1)
    td.line((0, 0, lw - 1, 0), fill='#3a5130')
    td.line((0, lh - 1, lw - 1, lh - 1), fill='#101c12')
    tex = tex.resize((width, height), Image.Resampling.NEAREST)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((x - 3, y - 3, x + width + 2, y + height + 2),
                           radius=4, fill='#101b12')
    canvas.paste(tex, (x, y))
    draw.line((x + 2, y, x + width - 3, y), fill='#526a3b', width=1)
    draw.line((x + 2, y + height - 1, x + width - 3, y + height - 1),
              fill='#0e1a11', width=1)


async def approved_fitness_goals(pool, user_id):
    """Only show goals from the latest approved plan request."""
    async with pool.acquire() as con:
        rows = await con.fetch("""SELECT goal_text FROM fitness_goals
            WHERE discord_user_id=$1 AND status='approved'
              AND source_request_id=(SELECT MAX(source_request_id)
                  FROM fitness_goals WHERE discord_user_id=$1 AND status='approved')
            ORDER BY goal_number""", user_id)
    return [row['goal_text'] for row in rows]


def make_profile(name, xp, count, target, avatar_bytes=None, stats=None, goals=None):
    """Approved white Fitness Quest profile. Phase 3 stats intentionally remain zero."""
    from pathlib import Path
    W, H = 1280, 1530
    im = Image.new('RGB', (W, H), '#ffffff')
    d = ImageDraw.Draw(im)
    # Square Discord avatar frame, no circular clipping.
    d.rectangle((60, 48, 438, 426), fill='#000000')
    if avatar_bytes:
        try:
            av = Image.open(io.BytesIO(avatar_bytes)).convert('RGB')
            from PIL import ImageOps
            av = ImageOps.fit(av, (356, 356), method=Image.Resampling.LANCZOS)
            im.paste(av, (71, 59))
        except Exception:
            d.rectangle((71, 59, 427, 415), fill='#a1a1a1')
    else:
        d.rectangle((71, 59, 427, 415), fill='#a1a1a1')

    _fitted(d, '名前:', (491, 45, 680), 58, japanese=True)
    _fitted(d, name, (686, 63, 510), 40)

    # Pixel-textured Minecraft-style XP bar, drawn on a tiny grid and
    # enlarged with nearest-neighbor so the individual green pixels stay crisp.
    bar_x, bar_y, bar_w, bar_h = 492, 146, 634, 18
    level, title = level_and_title(xp)
    progress = 100 if level == 15 else int(xp % 100)
    _draw_minecraft_xp_bar(im, (bar_x, bar_y, bar_w, bar_h), progress)
    # Plain percentage text, vertically centered beside the XP bar.
    d.text((1143, bar_y + bar_h // 2), f'{progress}%',
           font=_profile_font(27, heavy=True), fill='#000000', anchor='lm')
    _fitted(d, f'Level: {level}', (490, 190, 715), 62)
    _fitted(d, f'Titel: {title}', (490, 268, 715), 61)

    _fitted(d, 'WEEKLY', (61, 455, 405), 82)
    _fitted(d, 'WORKOUTS', (63, 579, 275), 45)
    _fitted(d, f'{count} / {target if target else "—"}', (345, 582, 120), 39)
    weekly_earned = weekly_xp(count, target)
    _fitted(d, 'XP', (63, 650, 220), 47)
    _fitted(d, f'{weekly_earned} / 50', (290, 652, 180), 36)

    _fitted(d, 'sisyphus spotted', (108, 729, 355), 35)
    meme = Path(__file__).resolve().parent.parent / 'assets' / 'sisyphus_spotted.png'
    if meme.is_file():
        try:
            from PIL import ImageOps
            with Image.open(meme) as src:
                src = ImageOps.fit(src.convert('RGB'), (418, 332), method=Image.Resampling.LANCZOS)
                im.paste(src, (41, 791))
        except OSError:
            pass

    stats = stats or {}
    _steam_progress(d, 464, 'MINI QUEST PROGRESS', stats.get('mini', 0), 4)
    _steam_progress(d, 614, 'MAIN QUEST PROGRESS', stats.get('main', 0), 1)
    _steam_progress(d, 764, 'MILESTONES', stats.get('milestones', 0), 3)
    _steam_progress(d, 914, 'ACHIEVEMENTS', stats.get('achievements', 0), 20)
    # Approved fitness goals; separate from the three milestone checkpoints.
    # A fixed-height extension preserves the approved artwork above this section.
    d.line((60, 1192, 1220, 1192), fill='#d6d6d6', width=3)
    _fitted(d, 'FITNESS GOALS', (61, 1214, 1150), 56)
    goals = [str(g).strip() for g in (goals or []) if str(g).strip()][:3]
    if not goals:
        d.text((65, 1310), 'No structured goals approved yet.',
               font=_profile_font(27), fill='#666666')
    else:
        for idx, goal in enumerate(goals):
            # Two short lines per goal; truncation avoids overlapping adjacent goals.
            from PIL import ImageFont
            y = 1301 + idx * 72
            d.text((65, y), f'{idx+1}.', font=_profile_font(29, heavy=True), fill='#111111')
            words = goal.split()
            lines, current = [], ''
            for word in words:
                candidate = (current + ' ' + word).strip()
                if d.textbbox((0, 0), candidate, font=_profile_font(26))[2] > 1040 and current:
                    lines.append(current)
                    current = word
                else:
                    current = candidate
            if current: lines.append(current)
            for n, line in enumerate(lines[:2]):
                if n == 1 and len(lines) > 2:
                    while line and d.textbbox((0, 0), line + '…', font=_profile_font(26))[2] > 1030:
                        line = line[:-1]
                    line += '…'
                d.text((112, y + n*29), line, font=_profile_font(26), fill='#222222')
    out = io.BytesIO()
    im.save(out, 'PNG')
    out.seek(0)
    return out


async def ensure_journal(bot, pool, guild, user):
    async with pool.acquire() as con:
        channel_id = await con.fetchval('SELECT journal_channel_id FROM players WHERE discord_user_id=$1', user.id)
    category = guild.get_channel(JOURNAL_CATEGORY_ID) if JOURNAL_CATEGORY_ID else None
    if not isinstance(category, discord.CategoryChannel):
        raise RuntimeError('JOURNAL_CATEGORY_ID is not configured or the category is missing.')
    async with pool.acquire() as con:
        chosen_name = await con.fetchval('SELECT challenger_name FROM players WHERE discord_user_id=$1', user.id)
    if not chosen_name:
        raise RuntimeError('Challenger must complete /register first.')
    safe_name = re.sub(r'[^a-z0-9-]+', '-', chosen_name.lower()).strip('-')[:55] or 'challenger'
    name = f'{safe_name}-log'
    # Use a user-id marker in topic to prevent duplicate journals after a restart.
    marker = f'Fitness Quest journal for {chosen_name}'
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel is not None and not isinstance(channel, discord.TextChannel):
        channel = None
    if channel is None:
        channel = next((c for c in category.text_channels if c.name == name), None)
    if channel is not None and (channel.name != name or channel.topic != marker):
        await channel.edit(name=name, topic=marker, reason='Update Fitness Quest challenger name')
    if channel is None:
        channel = await guild.create_text_channel(name, category=category, topic=marker,
            reason='Approved Fitness Quest challenger journal')
    async with pool.acquire() as con:
        await con.execute('UPDATE players SET journal_channel_id=$1 WHERE discord_user_id=$2', channel.id, user.id)
    return channel



async def announce_xp(bot, pool, user_id, amount, reason):
    """Notify the player's existing journal; XP is awarded elsewhere."""
    if amount <= 0:
        return False
    async with pool.acquire() as con:
        journal_id = await con.fetchval(
            'SELECT journal_channel_id FROM players WHERE discord_user_id=$1', user_id)
    channel = bot.get_channel(int(journal_id)) if journal_id else None
    if not isinstance(channel, discord.TextChannel):
        return False
    try:
        await channel.send(
            f'✨ **+{amount} XP** — {discord.utils.escape_markdown(str(reason))}',
            allowed_mentions=discord.AllowedMentions.none())
        return True
    except (discord.Forbidden, discord.HTTPException):
        log.exception('Could not announce XP for %s', user_id)
        return False


def workout_embed(record):
    embed = discord.Embed(title='💪 ' + (record['title'] or f"{record['workout_type']} Workout"), color=discord.Color.purple())
    embed.add_field(name='Activity', value=record['workout_type'])
    embed.add_field(name='Duration', value=f"{record['duration_minutes']} minutes")
    embed.add_field(name='Date', value=str(record['workout_date']))
    if record['notes']:
        embed.add_field(name='Notes', value=record['notes'][:1024], inline=False)
    if record['image_discord_url']:
        embed.set_image(url=record['image_discord_url'])
    return embed


async def refresh_journal_post(bot, pool, workout_id, user_id):
    async with pool.acquire() as con:
        record = await con.fetchrow("""SELECT w.*,p.journal_channel_id FROM workouts w
            JOIN players p ON p.discord_user_id=w.discord_user_id
            WHERE w.id=$1 AND w.discord_user_id=$2""", workout_id, user_id)
    if not record or not record['public_message_id'] or not record['journal_channel_id']:
        return False
    channel = bot.get_channel(record['journal_channel_id'])
    if not isinstance(channel, discord.TextChannel):
        return False
    try:
        message = await channel.fetch_message(record['public_message_id'])
        await message.edit(embed=workout_embed(record))
        return True
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        log.warning('Could not update journal message for workout %s', workout_id)
        return False


async def publish_backlog(bot, pool, user_id):
    async with pool.acquire() as con:
        rows = await con.fetch("""SELECT id FROM workouts WHERE discord_user_id=$1
            AND visibility='public' AND public_message_id IS NULL ORDER BY created_at LIMIT 50""", user_id)
    for row in rows:
        try:
            await publish_journal(bot, pool, row['id'], user_id)
        except (discord.Forbidden, discord.HTTPException):
            log.exception('Could not publish pending workout')
            break


async def publish_journal(bot, pool, workout_id, user_id):
    async with pool.acquire() as con:
        record = await con.fetchrow('''SELECT w.*, p.journal_channel_id FROM workouts w
            JOIN players p ON p.discord_user_id=w.discord_user_id
            WHERE w.id=$1 AND w.discord_user_id=$2''', workout_id, user_id)
    if not record or record['visibility'] != 'public':
        return False
    if record['public_message_id']:
        return True
    channel = bot.get_channel(record['journal_channel_id']) if record['journal_channel_id'] else None
    if not isinstance(channel, discord.TextChannel):
        return False
    embed = workout_embed(record)
    msg = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    async with pool.acquire() as con:
        await con.execute('UPDATE workouts SET public_message_id=$1 WHERE id=$2 AND public_message_id IS NULL', msg.id, workout_id)
    return True


class ReviewView(discord.ui.View):
    def __init__(self, pool, bot, request_id):
        super().__init__(timeout=None)
        self.pool, self.bot, self.request_id = pool, bot, request_id
        for label, style, decision in [('Approve', discord.ButtonStyle.success, 'approved'),
                                        ('Reject', discord.ButtonStyle.danger, 'rejected')]:
            b = discord.ui.Button(label=label, style=style,
                                  custom_id=f'fitnessquest:plan:{decision}:{request_id}')
            async def click(interaction, status=decision):
                await self.review(interaction, status)
            b.callback = click
            self.add_item(b)

    async def review(self, interaction, status):
        if not interaction.guild or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message('Manage Server permission required.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        day = today()
        async with self.pool.acquire() as con:
            async with con.transaction():
                request = await con.fetchrow('SELECT * FROM plan_requests WHERE id=$1 FOR UPDATE', self.request_id)
                if not request or request['status'] != 'pending':
                    await interaction.followup.send('This request was already reviewed.', ephemeral=True)
                    return
                old = await con.fetchval('SELECT 1 FROM plan_history WHERE discord_user_id=$1 LIMIT 1', request['discord_user_id'])
                effective = monday(day) + dt.timedelta(days=7) if old else monday(day)
                if status == 'approved':
                    while await con.fetchval('''SELECT 1 FROM plan_history WHERE discord_user_id=$1 AND effective_date=$2''',
                                             request['discord_user_id'], effective):
                        effective += dt.timedelta(days=7)
                await con.execute('''UPDATE plan_requests SET status=$1, reviewed_at=now(), reviewed_by=$2,
                    effective_date=$3 WHERE id=$4''', status, interaction.user.id,
                    effective if status == 'approved' else None, self.request_id)
                await con.execute('''UPDATE fitness_goals SET status=$1 WHERE source_request_id=$2 AND status='pending' ''',status,self.request_id)
                if status == 'approved':
                    await con.execute('''UPDATE fitness_goals SET status='superseded'
                        WHERE discord_user_id=$1 AND source_request_id<>$2 AND status='approved' ''',request['discord_user_id'],self.request_id)
                    await con.execute('''INSERT INTO plan_history(discord_user_id,weekly_target,effective_date,source_request_id)
                        VALUES($1,$2,$3,$4) ON CONFLICT(discord_user_id,effective_date)
                        DO UPDATE SET weekly_target=EXCLUDED.weekly_target, source_request_id=EXCLUDED.source_request_id''',
                        request['discord_user_id'], request['weekly_target'], effective, self.request_id)
        journal_text = ''
        if status == 'approved' and interaction.guild:
            member = interaction.guild.get_member(request['discord_user_id'])
            if member is None:
                try:
                    member = await interaction.guild.fetch_member(request['discord_user_id'])
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None
            if member:
                try:
                    channel = await ensure_journal(self.bot, self.pool, interaction.guild, member)
                    journal_text = f' Journal: {channel.mention}'
                except Exception as e:
                    log.warning('Journal creation deferred: %s', e)
                    journal_text = ' Journal creation pending: check category and permissions.'
        # Approval notices are posted in journals, never DMs.
        if status == 'approved' and member:
            async with self.pool.acquire() as con:
                journal_id = await con.fetchval('SELECT journal_channel_id FROM players WHERE discord_user_id=$1', member.id)
            channel = interaction.guild.get_channel(journal_id) if journal_id else None
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(f'⚔️ **{discord.utils.escape_markdown(member.display_name)}**, your workout plan was approved! '
                                       f'Target: **{request["weekly_target"]} workouts/week**. Effective **{effective}**.',
                                       allowed_mentions=discord.AllowedMentions.none())
                    await publish_backlog(self.bot, self.pool, member.id)
                except (discord.Forbidden, discord.HTTPException):
                    log.exception('Could not post journal approval notification')
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass
        if status == 'approved':
            from fitness_quest import achievements
            await achievements.refresh(self.bot,self.pool,request['discord_user_id'])
        await interaction.followup.send(f'Plan {status}.' + journal_text, ephemeral=True)


class RegistrationModal(discord.ui.Modal, title='Join Fitness Quest'):
    name = discord.ui.TextInput(label='Challenger name', placeholder='Name shown on profiles and journals', min_length=2, max_length=32)

    def __init__(self, pool, bot, guided=False):
        super().__init__()
        self.pool, self.bot, self.guided = pool, bot, guided

    async def on_submit(self, interaction):
        name = str(self.name).strip()
        if not re.fullmatch(r'[\w][\w .-]{1,31}', name, flags=re.UNICODE):
            await interaction.response.send_message('Use 2–32 letters, numbers, spaces, periods, hyphens or underscores.', ephemeral=True)
            return
        slug = re.sub(r'[^a-z0-9-]+', '-', name.lower()).strip('-')[:55]
        if not slug:
            await interaction.response.send_message('Please include at least one Latin letter or number for the channel name.', ephemeral=True)
            return
        async with self.pool.acquire() as con:
            conflict = await con.fetchval("""SELECT 1 FROM players WHERE lower(challenger_name)=lower($1)
                AND discord_user_id<>$2""", name, interaction.user.id)
            if conflict:
                await interaction.response.send_message('That challenger name is already in use. Choose another.', ephemeral=True)
                return
            await con.execute("""INSERT INTO players(discord_user_id,display_name,challenger_name)
                VALUES($1,$2,$3) ON CONFLICT(discord_user_id) DO UPDATE SET
                display_name=excluded.display_name,challenger_name=excluded.challenger_name""",
                interaction.user.id, interaction.user.display_name, name)
            approved = await con.fetchval('SELECT 1 FROM plan_history WHERE discord_user_id=$1 LIMIT 1', interaction.user.id)
        msg = f'⚔️ Registered as **{discord.utils.escape_markdown(name)}**!'
        if approved and interaction.guild:
            try:
                journal = await ensure_journal(self.bot, self.pool, interaction.guild, interaction.user)
                msg += f' Your journal: {journal.mention}'
            except (RuntimeError, discord.Forbidden, discord.HTTPException) as exc:
                log.warning('Could not update journal name: %s', exc)
        elif not self.guided:
            msg += ' Next, submit your workout plan using `/set_goal`.'
        if self.guided:
            from fitness_quest import quest_progress
            await interaction.response.defer(ephemeral=True)
            await quest_progress.send_onboarding_progress(interaction, self.pool, self.bot, msg)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


class PlanModal(discord.ui.Modal, title='Request weekly workout plan'):
    target = discord.ui.TextInput(label='Workouts per week (1-14)', placeholder='3', max_length=2)
    description = discord.ui.TextInput(label='Workout plan', style=discord.TextStyle.paragraph,
                                      placeholder='Strength twice, cardio once', max_length=400)
    goal1 = discord.ui.TextInput(label='Fitness goal 1 (required)', placeholder='Run 5K without stopping', max_length=200)
    goal2 = discord.ui.TextInput(label='Fitness goal 2 (optional)', placeholder='Increase bench press', required=False, max_length=200)
    goal3 = discord.ui.TextInput(label='Fitness goal 3 (optional)', placeholder='Improve flexibility', required=False, max_length=200)

    def __init__(self, pool, bot, guided=False):
        super().__init__()
        self.pool, self.bot, self.guided = pool, bot, guided

    async def on_submit(self, interaction):
        try:
            target = int(str(self.target))
            if not 1 <= target <= 14:
                raise ValueError
        except ValueError:
            await interaction.response.send_message('Target must be between 1 and 14.', ephemeral=True)
            return
        if not APPROVAL_CHANNEL_ID:
            await interaction.response.send_message('Admin has not configured APPROVAL_CHANNEL_ID.', ephemeral=True)
            return
        channel = self.bot.get_channel(APPROVAL_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message('Approval channel not found. Ask an admin to check configuration.', ephemeral=True)
            return
        async with self.pool.acquire() as con:
            await con.execute('''INSERT INTO players(discord_user_id,display_name) VALUES($1,$2)
                ON CONFLICT(discord_user_id) DO UPDATE SET display_name=EXCLUDED.display_name''',
                interaction.user.id, interaction.user.display_name)
            existing = await con.fetchval('SELECT id FROM plan_requests WHERE discord_user_id=$1 AND status=\'pending\'', interaction.user.id)
            if existing:
                await interaction.response.send_message('You already have a pending plan request.', ephemeral=True)
                return
            goals = [g.strip() for g in (str(self.goal1), str(self.goal2), str(self.goal3)) if g.strip()]
            req = await con.fetchval('''INSERT INTO plan_requests(discord_user_id,weekly_target,description)
                VALUES($1,$2,$3) RETURNING id''', interaction.user.id, target, str(self.description))
            for idx, goal in enumerate(goals, 1):
                await con.execute('''INSERT INTO fitness_goals(discord_user_id,source_request_id,goal_number,goal_text,status)
                    VALUES($1,$2,$3,$4,'pending')''', interaction.user.id, req, idx, goal)
        async with self.pool.acquire() as con:
            chosen_name = await con.fetchval('SELECT challenger_name FROM players WHERE discord_user_id=$1', interaction.user.id)
        view = ReviewView(self.pool, self.bot, req)
        goal_lines = '\n'.join(f'{idx}. {discord.utils.escape_markdown(g)}' for idx,g in enumerate(goals,1))
        embed = discord.Embed(title=f'📋 Plan Request #{req}', description=f'**{discord.utils.escape_markdown(chosen_name or interaction.user.display_name)}** ({interaction.user.mention})\n**Target:** {target}/week\n**Plan:** {str(self.description)}\n**Fitness goals:**\n{goal_lines}', color=discord.Color.blurple())
        msg = await channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        async with self.pool.acquire() as con:
            await con.execute('UPDATE plan_requests SET admin_message_id=$1 WHERE id=$2', msg.id, req)
        if self.guided:
            from fitness_quest import quest_progress
            await interaction.response.defer(ephemeral=True)
            await quest_progress.send_onboarding_progress(interaction, self.pool, self.bot, 'Plan submitted for admin approval!')
        else:
            await interaction.response.send_message('Plan submitted for admin approval!', ephemeral=True)


def register(bot, get_pool):
    guild = discord.Object(id=GUILD_ID)

    @bot.tree.command(name='register', description='Choose your Fitness Quest challenger name', guild=guild)
    async def register_challenger(interaction: discord.Interaction):
        await interaction.response.send_modal(RegistrationModal(get_pool(), bot))

    @bot.tree.command(name='set_goal', description='Request an approved weekly workout target', guild=guild)
    async def set_goal(interaction: discord.Interaction):
        async with get_pool().acquire() as con:
            registered = await con.fetchval('SELECT challenger_name FROM players WHERE discord_user_id=$1', interaction.user.id)
        if not registered:
            await interaction.response.send_message('Please use `/register` to choose your challenger name first.', ephemeral=True)
            return
        await interaction.response.send_modal(PlanModal(get_pool(), bot))

    @bot.tree.command(name='plan_status', description='Check your workout plan approval status', guild=guild)
    async def plan_status(interaction: discord.Interaction):
        async with get_pool().acquire() as con:
            pending = await con.fetchrow("""SELECT weekly_target,description FROM plan_requests
                WHERE discord_user_id=$1 AND status='pending' ORDER BY id DESC LIMIT 1""", interaction.user.id)
            latest = await con.fetchrow("""SELECT weekly_target,status,effective_date FROM plan_requests
                WHERE discord_user_id=$1 ORDER BY id DESC LIMIT 1""", interaction.user.id)
        if pending:
            message = f"⏳ Pending admin approval: **{pending['weekly_target']} workouts/week**."
        elif latest:
            message = f"Your latest plan request was **{latest['status']}**."
            if latest['status'] == 'approved':
                message += f" Effective {latest['effective_date']}."
            elif latest['status'] == 'rejected':
                message += ' You can submit a new request with `/set_goal`.'
        else:
            message = 'No workout plan submitted yet. Use `/register` and `/set_goal`.'
        await interaction.response.send_message(message, ephemeral=True)

    @bot.tree.command(name='profile', description='Show your RPG profile and graphical XP bar', guild=guild)
    @app_commands.describe(member='Optional challenger to view')
    async def profile(interaction: discord.Interaction, member: discord.Member | None = None):
        await interaction.response.defer()
        pool = get_pool()
        subject = member or interaction.user
        from fitness_quest import achievements
        if member is None: await achievements.refresh(bot,pool,subject.id)
        xp = await achievements.total_xp(pool, subject.id)
        stats = await achievements.profile_stats(pool, subject.id)
        goals_list = await approved_fitness_goals(pool, subject.id)
        count, target, _ = await get_week(pool, subject.id, monday(today()))
        async with pool.acquire() as con:
            chosen_name = await con.fetchval('SELECT challenger_name FROM players WHERE discord_user_id=$1', subject.id)
        if not chosen_name:
            await interaction.followup.send('This challenger has not registered yet.')
            return
        avatar = None
        try:
            avatar = await subject.display_avatar.read()
        except discord.HTTPException:
            pass
        img = await asyncio.to_thread(make_profile, chosen_name, xp, count, target, avatar, stats, goals_list)
        await interaction.followup.send(file=discord.File(img, filename='fitness-quest-profile.png'))

    @bot.tree.command(name='setup_journal', description='Create or reconnect a challenger journal', guild=guild)
    @app_commands.default_permissions(manage_guild=True)
    async def setup_journal(interaction: discord.Interaction, member: discord.Member):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message('Manage Server permission required.', ephemeral=True)
            return
        async with get_pool().acquire() as con:
            exists = await con.fetchval('SELECT 1 FROM plan_history WHERE discord_user_id=$1', member.id)
        if not exists:
            await interaction.response.send_message('This challenger needs an approved plan first.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            channel = await ensure_journal(bot, get_pool(), interaction.guild, member)
            await publish_backlog(bot, get_pool(), member.id)
            await interaction.followup.send(f'Journal ready: {channel.mention}', ephemeral=True)
        except (RuntimeError, discord.Forbidden, discord.HTTPException) as e:
            await interaction.followup.send(f'Could not create journal: {e}', ephemeral=True)


    @bot.tree.command(name='goals', description='View your approved fitness goals', guild=guild)
    async def goals(interaction: discord.Interaction, member: discord.Member | None = None):
        subject = member or interaction.user
        async with get_pool().acquire() as con:
            rows = await con.fetch("""SELECT goal_number, goal_text FROM fitness_goals
                WHERE discord_user_id=$1 AND status='approved'
                AND source_request_id=(SELECT max(source_request_id) FROM fitness_goals
                    WHERE discord_user_id=$1 AND status='approved')
                ORDER BY goal_number""", subject.id)
        if not rows:
            message = 'No structured fitness goals saved yet. Existing approved plans still work; use `/set_goal` to submit 1–3 goals with your next plan request.'
        else:
            message = '**Fitness goals for ' + discord.utils.escape_markdown(subject.display_name) + '**\n' + '\n'.join(f"{r['goal_number']}. {discord.utils.escape_markdown(r['goal_text'])}" for r in rows)
        await interaction.response.send_message(message, ephemeral=True)


async def restore_reviews(bot, pool):
    async with pool.acquire() as con:
        rows = await con.fetch('SELECT id FROM plan_requests WHERE status=\'pending\'')
    for row in rows:
        bot.add_view(ReviewView(pool, bot, row['id']))
