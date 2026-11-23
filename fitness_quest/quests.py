# Fitness Quest Phase 3A: leaderboard, weekly quests, private reflections.
import datetime as dt
import os
import discord
from discord import app_commands
from fitness_quest import workouts
from fitness_quest import quest_reviews
from fitness_quest import achievements
from fitness_quest import quest_scheduler
from fitness_quest import season_gate
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")

GUILD_ID = int(os.environ['DISCORD_GUILD_ID'])
ORGANIZER_ID = int(os.getenv('ORGANIZER_DISCORD_ID') or '0')
QUEST_SUBMISSIONS_CHANNEL_ID = int(os.getenv('QUEST_SUBMISSIONS_CHANNEL_ID') or '0')
PRIVATE_QUEST_SUBMISSIONS_CHANNEL_ID = int(os.getenv('PRIVATE_QUEST_SUBMISSIONS_CHANNEL_ID') or '0')


def is_organizer(interaction):
    return bool(ORGANIZER_ID and interaction.user.id == ORGANIZER_ID)


def is_admin(interaction):
    return is_organizer(interaction) or bool(interaction.user.guild_permissions.manage_guild)


def safe(text):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(text)))


async def scores(pool):
    async with pool.acquire() as con:
        players = await con.fetch('''SELECT DISTINCT p.discord_user_id, COALESCE(p.challenger_name,p.display_name) AS name
            FROM players p JOIN plan_history h ON h.discord_user_id=p.discord_user_id''')
        extra = await con.fetch('''SELECT discord_user_id, SUM(xp)::integer AS xp FROM (
            SELECT s.discord_user_id, q.xp FROM quest_submissions s
            JOIN quests q ON q.id=s.quest_id WHERE s.status='accepted'
            UNION ALL
            SELECT discord_user_id, 10 AS xp FROM weekly_reflections
        ) v GROUP BY discord_user_id''')
    extra_map = {r['discord_user_id']:r['xp'] for r in extra}
    week = workouts.monday(workouts.today())
    result = []
    for p in players:
        uid = p['discord_user_id']
        workout = await workouts.workout_total(pool, uid)
        count, target, wxp = await workouts.get_week(pool, uid, week)
        async with pool.acquire() as con:
            week_extra = await con.fetchval('''SELECT COALESCE(SUM(xp),0)::integer FROM (
                SELECT q.xp FROM quest_submissions s JOIN quests q ON q.id=s.quest_id
                WHERE s.discord_user_id=$1 AND s.status='accepted' AND q.week_start=$2
                UNION ALL SELECT 10 FROM weekly_reflections WHERE discord_user_id=$1 AND week_start=$2
            ) t''', uid, week)
            history = await con.fetch('''SELECT h.effective_date,h.weekly_target FROM plan_history h
                WHERE h.discord_user_id=$1 ORDER BY effective_date''',uid)
            completed = await con.fetch('''SELECT date_trunc('week',workout_date::timestamp)::date AS week_start,
                count(*)::integer AS n FROM workouts WHERE discord_user_id=$1 GROUP BY 1''',uid)
        byweek = {r['week_start']:r['n'] for r in completed}
        # Count completed goal weeks; include weeks with no workouts, break streak at current
        # incomplete week (unless the week is still in progress, in which case use last week).
        start = history[0]['effective_date'] if history else week
        successful = 0
        cursor = workouts.monday(start)
        while cursor <= week:
            active = [h for h in history if h['effective_date'] <= cursor]
            if active and byweek.get(cursor,0) >= active[-1]['weekly_target']:
                successful += 1
            cursor += dt.timedelta(days=7)
        streak = 0
        cursor = week if target and count >= target else week-dt.timedelta(days=7)
        while cursor >= workouts.monday(start):
            active = [h for h in history if h['effective_date'] <= cursor]
            if not active or byweek.get(cursor,0) < active[-1]['weekly_target']:
                break
            streak += 1
            cursor -= dt.timedelta(days=7)
        milestone = await quest_reviews.milestone_xp(pool, uid)
        async with pool.acquire() as con:
            milestone_week = await con.fetchval('''SELECT count(*)*30 FROM personal_milestones
                WHERE discord_user_id=$1 AND completion_status='approved'
                AND completion_reviewed_at >= $2::date
                AND completion_reviewed_at < ($2::date + interval '7 days')''',uid,week)
        total = await achievements.total_xp(pool,uid)
        achievement_week = await achievements.xp_week(pool,uid,week)
        level,title = workouts.level_and_title(total)
        result.append(dict(uid=uid,name=p['name'],total=total,weekly=wxp+week_extra+int(milestone_week or 0)+achievement_week,
                           level=level,title=title,streak=streak,success=successful,
                           count=count,target=target or 0))
    return result


class RankingView(discord.ui.View):
    def __init__(self, pool, rows):
        super().__init__(timeout=900)
        self.pool=pool
        self.rows=rows
        self.set_active('overall')

    def set_active(self, mode):
        for button, name in ((self.overall, 'overall'), (self.weekly, 'weekly'), (self.consistency, 'consistency')):
            button.style = discord.ButtonStyle.primary if mode == name else discord.ButtonStyle.secondary

    def embed(self, mode):
        if mode=='overall':
            ordered=sorted(self.rows,key=lambda r:(-r['total'],r['name'].lower()))
            heading='🏆 Overall Rankings'
        elif mode=='weekly':
            ordered=sorted(self.rows,key=lambda r:(-r['weekly'],-r['total'],r['name'].lower()))
            heading='⚡ Weekly Rankings'
        else:
            ordered=sorted(self.rows,key=lambda r:(-r['success'],-r['streak'],-r['total'],r['name'].lower()))
            heading='🔥 Consistency Rankings'
        embed=discord.Embed(title=heading,description='Fitness Quest · Week of '+str(workouts.monday(workouts.today())),color=0x8876c9)
        if not ordered:
            embed.description+='\nNo approved challengers yet.'
        for i,r in enumerate(ordered[:15]):
            medal=['🥇','🥈','🥉'][i] if i<3 else f'#{i+1}'
            if mode=='overall':
                value=f"**{r['total']} XP** · Lv. {r['level']} {r['title']} · 🔥 {r['streak']} wk"
            elif mode=='weekly':
                value=f"**{r['weekly']} XP** this week · 💪 {r['count']}/{r['target']} workouts"
            else:
                value=f"**{r['success']} goal weeks** · 🔥 {r['streak']}-week streak"
            embed.add_field(name=f"{medal} {safe(r['name'])}",value=value,inline=False)
        embed.set_footer(text='Workouts + quest submissions + reflection XP + approved milestone XP')
        return embed

    @discord.ui.button(label='Overall',emoji='🏆',style=discord.ButtonStyle.primary)
    async def overall(self, interaction:discord.Interaction,button:discord.ui.Button):
        self.set_active('overall')
        await interaction.response.edit_message(embed=self.embed('overall'),view=self)

    @discord.ui.button(label='Weekly',emoji='⚡',style=discord.ButtonStyle.secondary)
    async def weekly(self, interaction:discord.Interaction,button:discord.ui.Button):
        self.set_active('weekly')
        await interaction.response.edit_message(embed=self.embed('weekly'),view=self)

    @discord.ui.button(label='Consistency',emoji='🔥',style=discord.ButtonStyle.secondary)
    async def consistency(self, interaction:discord.Interaction,button:discord.ui.Button):
        self.set_active('consistency')
        await interaction.response.edit_message(embed=self.embed('consistency'),view=self)


class ReflectionModal(discord.ui.Modal,title='Weekly Reflection'):
    reflection=discord.ui.TextInput(label='How did your week go?',style=discord.TextStyle.paragraph,max_length=1500)
    next_week=discord.ui.TextInput(label='What will you focus on next week?',style=discord.TextStyle.paragraph,max_length=500)

    def __init__(self,pool):
        super().__init__(); self.pool=pool

    async def on_submit(self,interaction:discord.Interaction):
        if not await season_gate.require_open(interaction):
            return
        week=workouts.monday(workouts.today())
        async with self.pool.acquire() as con:
            exists=await con.fetchval('SELECT 1 FROM players WHERE discord_user_id=$1',interaction.user.id)
            if not exists:
                await interaction.response.send_message('Register with `/register` first.',ephemeral=True); return
            result=await con.execute('''INSERT INTO weekly_reflections(week_start,discord_user_id,response)
                VALUES($1,$2,$3) ON CONFLICT(week_start,discord_user_id) DO NOTHING''',
                week,interaction.user.id,f"This week: {self.reflection.value}\nNext week: {self.next_week.value}")
        if result == 'INSERT 0 1':
            await interaction.response.defer(ephemeral=True)
            unlocked = await achievements.refresh(interaction.client, self.pool, interaction.user.id)
            await workouts.announce_xp(interaction.client, self.pool, interaction.user.id,
                                       10, 'Weekly reflection submitted', unlocked)
        else:
            await interaction.response.send_message('You already submitted a reflection this week. No duplicate XP awarded.', ephemeral=True)



class QuestModal(discord.ui.Modal):
    def __init__(self, pool, quest):
        super().__init__(title=('Submit: ' + quest['title'])[:45])
        self.pool, self.quest = pool, quest
        proof = quest['proof_type']
        self.answer = discord.ui.TextInput(style=discord.TextStyle.paragraph,
            required=proof == 'text', max_length=1400, placeholder='Describe your completion')
        self.add_item(discord.ui.Label(text='Response', component=self.answer))
        self.visibility = discord.ui.Select(options=[
            discord.SelectOption(label='Share publicly', value='public', default=True),
            discord.SelectOption(label="Don't Share Publicly", value='private')],
            min_values=1, max_values=1)
        self.add_item(discord.ui.Label(text='Submission visibility', component=self.visibility))
        self.upload = None
        if proof != 'text':
            self.upload = discord.ui.FileUpload(required=proof == 'image',
                min_values=1 if proof == 'image' else 0, max_values=1)
            self.add_item(discord.ui.Label(text='Screenshot proof', component=self.upload))

    async def on_submit(self, interaction):
        if not await season_gate.require_open(interaction):
            return
        q = self.quest
        response = self.answer.value.strip()
        files = (self.upload.values or []) if self.upload else []
        if q['proof_type'] == 'text' and not response:
            return await interaction.response.send_message('Text is required.', ephemeral=True)
        if q['proof_type'] == 'image' and not files:
            return await interaction.response.send_message('Screenshot required.', ephemeral=True)
        if q['proof_type'] == 'either' and not (response or files):
            return await interaction.response.send_message('Provide text or screenshot.', ephemeral=True)
        attachment = files[0] if files else None
        if attachment and (not (attachment.content_type or '').startswith('image/') or attachment.size > 8*1024*1024):
            return await interaction.response.send_message('Use an image smaller than 8 MB.', ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        week = workouts.monday(workouts.today())
        async with self.pool.acquire() as con:
            player = await con.fetchval('SELECT 1 FROM players WHERE discord_user_id=$1', interaction.user.id)
            active = await con.fetchrow('''SELECT id,kind,published,release_date,due_date FROM quests
                WHERE id=$1 AND week_start=$2''', q['id'], week)
            exception = await con.fetchval('''SELECT 1 FROM quest_deadline_exceptions
                WHERE quest_id=$1 AND discord_user_id=$2''', q['id'], interaction.user.id)
            previous = await con.fetchval('SELECT 1 FROM quest_submissions WHERE quest_id=$1 AND discord_user_id=$2', q['id'], interaction.user.id)
        if not player or not active or not active['published']:
            return await interaction.followup.send('Register first and select an active quest.', ephemeral=True)
        if not quest_scheduler.quest_open(active) and not (exception and active['release_date'] and dt.datetime.now(TZ).date() >= active['release_date']):
            return await interaction.followup.send('This quest is not currently open. Ask an organizer for a late-submission exception if its deadline passed.', ephemeral=True)
        if previous:
            return await interaction.followup.send('Already submitted; no duplicate XP.', ephemeral=True)
        private = self.visibility.values[0] == 'private'
        channel_id = PRIVATE_QUEST_SUBMISSIONS_CHANNEL_ID if private else QUEST_SUBMISSIONS_CHANNEL_ID
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return await interaction.followup.send('Private organizer channel not configured.' if private else 'Quest submission channel unavailable.', ephemeral=True)
        if private and isinstance(channel, discord.TextChannel):
            everyone = interaction.guild.default_role
            if channel.permissions_for(everyone).view_channel:
                return await interaction.followup.send('Private channel is visible to @everyone; ask an organizer to fix its permissions.', ephemeral=True)
        embed = discord.Embed(title='Quest completed: ' + q['title'],
            description=safe(response) if response else 'Screenshot submission', color=0x8876c9)
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        if private:
            embed.set_footer(text='Private submission — organizer access only')
        send_kwargs = {}
        if attachment:
            ext = attachment.filename.rsplit('.',1)[-1].lower() if '.' in attachment.filename else 'png'
            if ext not in ('png','jpg','jpeg','webp','gif'): ext = 'png'
            try:
                upload_file = await attachment.to_file(filename='proof.'+ext)
            except discord.HTTPException:
                return await interaction.followup.send('Unable to read image; try again.', ephemeral=True)
            send_kwargs['file'] = upload_file
            embed.set_image(url='attachment://' + upload_file.filename)
        try:
            message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none(), **send_kwargs)
        except (discord.HTTPException, discord.Forbidden):
            return await interaction.followup.send('Could not post to quest submissions channel.', ephemeral=True)
        try:
            async with self.pool.acquire() as con:
                allowed = await con.fetchval('''SELECT 1 FROM quests q WHERE q.id=$1 AND q.published=true
                    AND q.release_date <= $3 AND (q.due_date >= $3 OR EXISTS (
                    SELECT 1 FROM quest_deadline_exceptions x
                    WHERE x.quest_id=q.id AND x.discord_user_id=$2))''',
                    q['id'], interaction.user.id, dt.datetime.now(TZ).date())
                if not allowed:
                    try: await message.delete()
                    except discord.HTTPException: pass
                    return await interaction.followup.send('The quest deadline has passed. Request an organizer exception.',ephemeral=True)
                result = await con.execute(
                    'INSERT INTO quest_submissions(quest_id,discord_user_id,response) VALUES($1,$2,$3) ON CONFLICT(quest_id,discord_user_id) DO NOTHING',
                    q['id'], interaction.user.id, (response or '[Screenshot]') + ' | Proof: ' + message.jump_url)
        except Exception:
            try: await message.delete()
            except discord.HTTPException: pass
            raise
        if result != 'INSERT 0 1':
            try: await message.delete()
            except discord.HTTPException: pass
            return await interaction.followup.send('Already submitted; no duplicate XP.', ephemeral=True)
        unlocked = await achievements.refresh(interaction.client,self.pool,interaction.user.id)
        await workouts.announce_xp(interaction.client, self.pool, interaction.user.id,
                                   int(q['xp']), f"Quest completed: {q['title']}", unlocked)
        pass  # Interaction acknowledged by defer; no hub follow-up.


class QuestSelect(discord.ui.Select):
    def __init__(self, pool, quests):
        self.pool = pool
        self.quests = {str(q['id']):q for q in quests}
        options = [discord.SelectOption(label=q['title'][:100], value=str(q['id']),
                   description=f"{q['xp']} XP / {q['proof_type']}") for q in quests[:25]]
        super().__init__(placeholder='Choose a quest', options=options)

    async def callback(self, interaction):
        await interaction.response.send_modal(QuestModal(self.pool, self.quests[self.values[0]]))


class QuestSelectView(discord.ui.View):
    def __init__(self, pool, quests):
        super().__init__(timeout=300)
        self.add_item(QuestSelect(pool, quests))


def preseason_test_week(day):
    """Explicit one-week test allowance; never changes the 12-week season."""
    return False  # Preseason test publishing is permanently disabled.


def register(bot,get_pool):
    guild=discord.Object(id=GUILD_ID)

    @bot.tree.command(name='leaderboard',description='Public overall, weekly and consistency rankings',guild=guild)
    async def leaderboard(interaction:discord.Interaction):
        await interaction.response.defer()
        rows=await scores(get_pool())
        view=RankingView(get_pool(),rows)
        await interaction.followup.send(embed=view.embed('overall'),view=view)

    @bot.tree.command(name='quest_add',description='Admin: create a draft quest for a week',guild=guild)
    @app_commands.describe(week_start='Monday date YYYY-MM-DD',kind='Mini (5 XP) or main (20 XP)',title='Quest title',instructions='Quest instructions',proof_type='Proof requirement')
    @app_commands.choices(kind=[app_commands.Choice(name='Mini quest',value='mini'),app_commands.Choice(name='Main quest',value='main')],
        proof_type=[app_commands.Choice(name='Text',value='text'),app_commands.Choice(name='Screenshot',value='image'),app_commands.Choice(name='Either',value='either')])
    async def quest_add(interaction:discord.Interaction,week_start:str,kind:app_commands.Choice[str],title:str,instructions:str,proof_type:app_commands.Choice[str]=None,is_team:bool=False):
        if not is_admin(interaction):
            await interaction.response.send_message('Admins only.',ephemeral=True);return
        try:
            day=dt.date.fromisoformat(week_start)
            if day.weekday()!=0: raise ValueError
        except ValueError:
            await interaction.response.send_message('Use a Monday in YYYY-MM-DD format.',ephemeral=True);return
        async with get_pool().acquire() as con:
            async with con.transaction():
                start = quest_scheduler.season_start()
                is_test = preseason_test_week(day)
                if not is_test and (start is None or not start <= day < start + dt.timedelta(weeks=12)):
                    await interaction.response.send_message('Quest date must fall within the configured 12-week season.',ephemeral=True); return
                number = 1 if is_test else (day-start).days//7 + 1
                chapter = 1 if is_test else (number-1)//4 + 1
                await con.execute('''INSERT INTO quest_weeks(week_start,chapter,week_number) VALUES($1,$2,$3)
                    ON CONFLICT(week_start) DO UPDATE SET chapter=EXCLUDED.chapter,week_number=EXCLUDED.week_number''',day,chapter,number)
                n=await con.fetchval('SELECT count(*) FROM quests WHERE week_start=$1 AND kind=$2',day,kind.value)
                if (kind.value=='mini' and n>=4) or (kind.value=='main' and n>=1):
                    await interaction.response.send_message('That week already has the maximum number of quests of this type.',ephemeral=True);return
                row=await con.fetchrow('''INSERT INTO quests(week_start,kind,title,instructions,xp,proof_type,is_team)
                    VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id''',day,kind.value,title[:100],instructions[:1500],5 if kind.value=='mini' else 20,proof_type.value if proof_type else 'text',is_team)
        await interaction.response.send_message(f"Draft quest #{row['id']} created. Publish with `/quest_publish`.",ephemeral=True)

    @bot.tree.command(name='quest_publish',description='Admin: publish a draft quest',guild=guild)
    async def quest_publish(interaction:discord.Interaction,quest_id:int):
        if not is_admin(interaction):
            await interaction.response.send_message('Admins only.',ephemeral=True);return
        async with get_pool().acquire() as con:
            async with con.transaction():
                row=await con.fetchrow('SELECT id,title,week_start,published FROM quests WHERE id=$1 FOR UPDATE',quest_id)
                if not row:
                    message='Quest not found.'
                elif not preseason_test_week(row['week_start']):
                    message='Manual publishing is disabled for scheduled quests. They release automatically on their assigned day.'
                elif row['published']:
                    message=f"Test quest #{quest_id} is already published."
                else:
                    today=dt.datetime.now(TZ).date()
                    end=row['week_start']+dt.timedelta(days=6)
                    if not row['week_start'] <= today <= end:
                        message='Test quest week has ended or not begun; cannot publish it now.'
                    else:
                        await con.execute("""UPDATE quests SET published=true,release_date=$2,due_date=$3
                            WHERE id=$1""",quest_id,today,end)
                        message=f"Test quest #{quest_id} published. Open /quests to submit; deadline Sunday 11:59 PM Eastern."
        await interaction.response.send_message(message,ephemeral=True)

    @bot.tree.command(name='quest_exception',description='Admin: permit a late submission for one quest',guild=guild)
    async def quest_exception(interaction:discord.Interaction,member:discord.Member,quest_id:int):
        if not is_admin(interaction):
            return await interaction.response.send_message('Admins only.',ephemeral=True)
        async with get_pool().acquire() as con:
            quest=await con.fetchrow('SELECT id,release_date,due_date FROM quests WHERE id=$1',quest_id)
            if not quest or not quest['release_date']:
                return await interaction.response.send_message('Quest not found or not yet scheduled.',ephemeral=True)
            await con.execute('''INSERT INTO quest_deadline_exceptions(quest_id,discord_user_id,approved_by)
                VALUES($1,$2,$3) ON CONFLICT DO NOTHING''',quest_id,member.id,interaction.user.id)
        await interaction.response.send_message(f'Late submission authorized for {member.display_name} on quest #{quest_id}.',ephemeral=True)

    @bot.tree.command(name='quests',description='View published quests for the current week',guild=guild)
    async def quests(interaction:discord.Interaction):
        week=workouts.monday(workouts.today())
        async with get_pool().acquire() as con:
            rows=await con.fetch('''SELECT id,kind,title,instructions,xp,proof_type FROM quests
                WHERE week_start=$1 AND published=true AND release_date <= $2 AND due_date >= $2 ORDER BY kind,id''',week,dt.datetime.now(TZ).date())
        e=discord.Embed(title='📜 Weekly Quest Board',description=f'Week of {week}',color=0x8876c9)
        for r in rows:
            e.add_field(name=f"{'⚔️' if r['kind']=='main' else '🎨'} #{r['id']} {safe(r['title'])} · {r['xp']} XP",
                        value=safe(r['instructions'])[:900]+'\nProof: '+r['proof_type'],inline=False)
        if not rows:e.description+='\nNo published quests yet.'
        await interaction.response.send_message(embed=e,view=QuestSelectView(get_pool(),rows) if rows else None)

    @bot.tree.command(name='quest_submit',description='Open quest submission form',guild=guild)
    async def quest_submit(interaction:discord.Interaction,quest_id:int):
        if not await season_gate.require_open(interaction):
            return
        week=workouts.monday(workouts.today())
        async with get_pool().acquire() as con:
            q=await con.fetchrow('''SELECT q.id,q.title,q.xp,q.proof_type FROM quests q
                WHERE q.id=$1 AND q.published=true AND q.release_date <= $3
                AND (q.week_start=$2 AND q.due_date >= $3 OR EXISTS (
                    SELECT 1 FROM quest_deadline_exceptions x
                    WHERE x.quest_id=q.id AND x.discord_user_id=$4))''',
                quest_id,week,dt.datetime.now(TZ).date(),interaction.user.id)
        if not q:
            return await interaction.response.send_message('Quest not found or inactive.',ephemeral=True)
        await interaction.response.send_modal(QuestModal(get_pool(),q))

    @bot.tree.command(name='quest_log',description='View your completed quests this week',guild=guild)
    async def quest_log(interaction:discord.Interaction):
        week=workouts.monday(workouts.today())
        async with get_pool().acquire() as con:
            rows=await con.fetch('''SELECT q.title,q.xp,s.status FROM quests q
                LEFT JOIN quest_submissions s ON s.quest_id=q.id AND s.discord_user_id=$1
                WHERE q.week_start=$2 AND q.published=true AND q.release_date <= $3 ORDER BY q.id''',interaction.user.id,week,dt.datetime.now(TZ).date())
        lines=[f"{'✅' if r['status']=='accepted' else '⬜'} {safe(r['title'])} (+{r['xp']} XP)" for r in rows]
        await interaction.response.send_message('📜 **Your Quest Log**\n'+('\n'.join(lines) if lines else 'No active quests.'),ephemeral=True)

    @bot.tree.command(name='reflect',description='Submit a private weekly reflection for 10 XP',guild=guild)
    async def reflect(interaction:discord.Interaction):
        if not await season_gate.require_open(interaction):
            return
        await interaction.response.send_modal(ReflectionModal(get_pool()))

    @bot.tree.command(name='reflections',description='Organizer: privately review reflections by week',guild=guild)
    async def reflections(interaction:discord.Interaction,week_start:str):
        if not is_organizer(interaction):
            await interaction.response.send_message('Only the designated organizer can view reflections.',ephemeral=True);return
        try: week=dt.date.fromisoformat(week_start)
        except ValueError:
            await interaction.response.send_message('Use YYYY-MM-DD (Monday).',ephemeral=True);return
        if week.weekday()!=0:
            await interaction.response.send_message('Use the Monday of the week.',ephemeral=True);return
        async with get_pool().acquire() as con:
            rows=await con.fetch('''SELECT p.challenger_name,p.display_name,r.response FROM weekly_reflections r
                JOIN players p ON p.discord_user_id=r.discord_user_id
                WHERE r.week_start=$1 ORDER BY r.submitted_at''',week)
        if not rows:
            await interaction.response.send_message('No reflections for that week.',ephemeral=True);return
        await interaction.response.defer(ephemeral=True)
        pages=[]
        for r in rows:
            name=safe(r['challenger_name'] or r['display_name'])
            pages.append(f"**{name}**\n{safe(r['response'])}")
        # Discord 2000 character message limit; chunk by reflection, never post publicly.
        current=''
        for part in pages:
            if len(part)>1850: part=part[:1845]+'…'
            if len(current)+len(part)+2>1850 and current:
                await interaction.followup.send(current,ephemeral=True)
                current=''
            current+=(('\n\n' if current else '')+part)
        if current:
            await interaction.followup.send(current,ephemeral=True)

    @bot.tree.command(name='my_reflection',description='Privately review your reflection for the current week',guild=guild)
    async def my_reflection(interaction:discord.Interaction):
        week=workouts.monday(workouts.today())
        async with get_pool().acquire() as con:
            response=await con.fetchval('SELECT response FROM weekly_reflections WHERE discord_user_id=$1 AND week_start=$2',interaction.user.id,week)
        await interaction.response.send_message('📖 **Your Reflection**\n'+safe(response) if response else 'No reflection submitted this week.',ephemeral=True)

    @bot.tree.command(name='quest_revoke',description='Admin: revoke XP from a quest submission',guild=guild)
    async def quest_revoke(interaction:discord.Interaction,quest_id:int,member:discord.Member):
        if not is_admin(interaction):
            await interaction.response.send_message('Admins only.',ephemeral=True);return
        async with get_pool().acquire() as con:
            row=await con.fetchrow("UPDATE quest_submissions SET status='revoked' WHERE quest_id=$1 AND discord_user_id=$2 AND status='accepted' RETURNING id",quest_id,member.id)
        await interaction.response.send_message('Submission revoked; XP no longer counts.' if row else 'No active submission found.',ephemeral=True)
