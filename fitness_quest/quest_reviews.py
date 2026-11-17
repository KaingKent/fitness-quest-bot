
"""Phase 3B personal milestone proposals, proof, organizer approvals and XP."""
import os
import datetime as dt
import discord
from discord import app_commands
from fitness_quest import workouts

GUILD_ID = int(os.environ['DISCORD_GUILD_ID'])
ORGANIZER_ID = int(os.getenv('ORGANIZER_DISCORD_ID') or '0')

def admin(i):
    return (bool(ORGANIZER_ID) and i.user.id == ORGANIZER_ID) or i.user.guild_permissions.manage_guild

def escape(value):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(value)))

async def player_exists(pool, uid):
    async with pool.acquire() as c:
        return bool(await c.fetchval('SELECT 1 FROM players WHERE discord_user_id=$1',uid))

async def milestone_xp(pool, uid):
    async with pool.acquire() as c:
        return int(await c.fetchval("SELECT count(*)*30 FROM personal_milestones WHERE discord_user_id=$1 AND completion_status='approved'",uid) or 0)

class SetMilestones(discord.ui.Modal, title='Set Your 3 Milestones'):
    first=discord.ui.TextInput(label='Chapter I / Week 4',style=discord.TextStyle.paragraph,max_length=300)
    second=discord.ui.TextInput(label='Chapter II / Week 8',style=discord.TextStyle.paragraph,max_length=300)
    third=discord.ui.TextInput(label='Chapter III / Week 12',style=discord.TextStyle.paragraph,max_length=300)
    def __init__(self,pool,guided=False):
        super().__init__()
        self.pool=pool
        self.guided=guided
    async def on_submit(self,i):
        await i.response.defer(ephemeral=True)
        if not await player_exists(self.pool,i.user.id):
            return await i.followup.send('Use /register first.',ephemeral=True)
        goals=[self.first.value.strip(),self.second.value.strip(),self.third.value.strip()]
        if any(not g for g in goals):
            return await i.followup.send('All three goals are required.',ephemeral=True)
        async with self.pool.acquire() as c:
            async with c.transaction():
                existing=await c.fetchval('SELECT count(*) FROM personal_milestones WHERE discord_user_id=$1',i.user.id)
                if existing:
                    return await i.followup.send('Milestones already exist. Ask an admin to adjust them.',ephemeral=True)
                for chapter,goal in enumerate(goals,1):
                    await c.execute('INSERT INTO personal_milestones(discord_user_id,chapter,goal) VALUES($1,$2,$3)',i.user.id,chapter,goal)
        from fitness_quest import quest_progress
        for chapter in (1,2,3):
            try:
                await quest_progress.post_milestone_review(i.client,self.pool,i.user.id,chapter,'goal')
            except (discord.HTTPException,RuntimeError) as exc:
                await i.followup.send(f'Goals saved, but approval notification failed: {exc}',ephemeral=True)
                return
        if self.guided:
            await quest_progress.send_onboarding_progress(i, self.pool, i.client, 'All three goals sent to #plan-approvals. No XP yet.')
        else:
            pass  # Deferred interaction already acknowledged; no hub success message.

class MilestoneProof(discord.ui.Modal):
    def __init__(self,pool,row):
        super().__init__(title='Complete milestone '+str(row['chapter']))
        self.pool,self.row=pool,row
        self.details=discord.ui.TextInput(style=discord.TextStyle.paragraph,max_length=1000,
                                           placeholder='Explain what you accomplished')
        self.upload=discord.ui.FileUpload(required=False,min_values=0,max_values=1)
        self.add_item(discord.ui.Label(text='Completion details',component=self.details))
        self.add_item(discord.ui.Label(text='Optional screenshot',component=self.upload))
    async def on_submit(self,i):
        uploads=self.upload.values or []
        attachment=uploads[0] if uploads else None
        if attachment and (not (attachment.content_type or '').startswith('image/') or attachment.size>8*1024*1024):
            return await i.response.send_message('Attach an image smaller than 8 MB.',ephemeral=True)
        await i.response.defer(ephemeral=True)
        proof_url=None
        # Milestone evidence is kept out of public quest channels.
        # Discord attachment URLs are not durable storage; save a private admin message.
        if attachment:
            channel_id=int(os.getenv('APPROVAL_CHANNEL_ID') or '0')
            channel=i.guild.get_channel(channel_id) if channel_id else None
            if not channel:
                return await i.followup.send('Admin approval channel unavailable. Ask the organizer to configure it.',ephemeral=True)
            try:
                file=await attachment.to_file(filename='milestone-proof.'+(attachment.filename.rsplit('.',1)[-1].lower() if '.' in attachment.filename else 'png'))
                message=await channel.send(content=f'Milestone proof from <@{i.user.id}> · Chapter {self.row["chapter"]}',
                                           file=file,allowed_mentions=discord.AllowedMentions.none())
                proof_url=message.jump_url
            except discord.HTTPException:
                return await i.followup.send('Could not save proof; try again.',ephemeral=True)
        async with self.pool.acquire() as c:
            updated=await c.fetchrow("""UPDATE personal_milestones SET completion_status='pending',
                completion_note=$3, proof_message_url=$4, completion_submitted_at=now()
                WHERE discord_user_id=$1 AND chapter=$2 AND goal_status='approved'
                  AND completion_status IN ('not_submitted','rejected')
                RETURNING chapter""",i.user.id,self.row['chapter'],self.details.value.strip(),proof_url)
        if not updated:
            return await i.followup.send('This milestone is not approved, or its completion is already pending/approved.',ephemeral=True)
        from fitness_quest import quest_progress
        try:
            await quest_progress.post_milestone_review(i.client,self.pool,i.user.id,self.row['chapter'],'completion')
        except (discord.HTTPException,RuntimeError) as exc:
            await i.followup.send(f'Completion saved, but approval notification failed: {exc}',ephemeral=True)
            return
        pass  # No hub success confirmation; organizer review remains in #plan-approvals.

def register(bot,get_pool):
    guild=discord.Object(id=GUILD_ID)

    @bot.tree.command(name='milestones',description='View your milestones or another challenger’s milestones',guild=guild)
    async def milestones(i:discord.Interaction,member:discord.Member|None=None):
        uid=member.id if member else i.user.id
        async with get_pool().acquire() as c:
            rows=await c.fetch('SELECT chapter,goal,goal_status,completion_status FROM personal_milestones WHERE discord_user_id=$1 ORDER BY chapter',uid)
        if not rows:
            return await i.response.send_message('No milestones yet. Use /milestone_set to propose all three.',ephemeral=True)
        e=discord.Embed(title='🏁 Personal Milestones',color=0x8876c9)
        for r in rows:
            e.add_field(name=f'Chapter {r["chapter"]} · Week {r["chapter"]*4}',
                value=f'{escape(r["goal"])}\\nGoal: {r["goal_status"]} · Completion: {r["completion_status"]} · 30 XP',inline=False)
        if member is None:
            async with get_pool().acquire() as c:
                reasons=await c.fetch('''SELECT DISTINCT ON (chapter,kind) chapter,kind,reason
                    FROM milestone_rejections WHERE discord_user_id=$1 ORDER BY chapter,kind,id DESC''',uid)
            for r in reasons:
                e.add_field(name=f'Chapter {r["chapter"]} {r["kind"]} rejection',value=escape(r['reason']),inline=False)
        await i.response.send_message(embed=e,ephemeral=member is None)

    @bot.tree.command(name='milestone_set',description='Propose three personal fitness milestones',guild=guild)
    async def milestone_set(i:discord.Interaction):
        await i.response.send_modal(SetMilestones(get_pool()))

    @bot.tree.command(name='milestone_submit',description='Submit proof for an approved milestone',guild=guild)
    @app_commands.describe(chapter='1, 2 or 3; early completion is allowed')
    async def milestone_submit(i:discord.Interaction,chapter:app_commands.Range[int,1,3]):
        async with get_pool().acquire() as c:
            row=await c.fetchrow('SELECT chapter,goal_status,completion_status FROM personal_milestones WHERE discord_user_id=$1 AND chapter=$2',i.user.id,chapter)
        if not row or row['goal_status']!='approved' or row['completion_status'] not in ('not_submitted','rejected'):
            return await i.response.send_message('Milestone must be goal-approved and not already pending or completed.',ephemeral=True)
        await i.response.send_modal(MilestoneProof(get_pool(),row))

    @bot.tree.command(name='milestone_review',description='Admin: view pending milestone goals and completion proofs',guild=guild)
    async def milestone_review(i:discord.Interaction):
        if not admin(i):
            return await i.response.send_message('Admins only.',ephemeral=True)
        async with get_pool().acquire() as c:
            rows=await c.fetch("""SELECT m.discord_user_id,m.chapter,m.goal,m.goal_status,m.completion_status,
                m.completion_note,m.proof_message_url,COALESCE(p.challenger_name,p.display_name) AS name
                FROM personal_milestones m JOIN players p ON p.discord_user_id=m.discord_user_id
                WHERE m.goal_status='pending' OR m.completion_status='pending'
                ORDER BY m.discord_user_id,m.chapter LIMIT 20""")
        if not rows:
            return await i.response.send_message('No pending milestone reviews.',ephemeral=True)
        lines=[]
        for r in rows:
            kind='GOAL' if r['goal_status']=='pending' else 'COMPLETION'
            line=f"**{kind}** · {escape(r['name'])} (<@{r['discord_user_id']}>) · chapter {r['chapter']}\\n{escape(r['goal'])}"
            if kind=='COMPLETION':
                line+='\\nProof: '+escape((r['completion_note'] or '')[:300])
                if r['proof_message_url']: line+='\\n'+r['proof_message_url']
            lines.append(line)
        await i.response.send_message('\\n\\n'.join(lines)[:1950],ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    @bot.tree.command(name='milestone_goal_review',description='Admin: approve or reject a proposed milestone goal',guild=guild)
    @app_commands.choices(decision=[app_commands.Choice(name='Approve',value='approved'),app_commands.Choice(name='Reject',value='rejected')])
    async def milestone_goal_review(i:discord.Interaction,member:discord.Member,chapter:app_commands.Range[int,1,3],decision:app_commands.Choice[str]):
        if not admin(i):
            return await i.response.send_message('Admins only.',ephemeral=True)
        async with get_pool().acquire() as c:
            row=await c.fetchrow("UPDATE personal_milestones SET goal_status=$3,goal_reviewed_at=now(),goal_reviewer_id=$4 WHERE discord_user_id=$1 AND chapter=$2 AND goal_status='pending' RETURNING chapter",member.id,chapter,decision.value,i.user.id)
        await i.response.send_message(f'Goal {decision.value}.' if row else 'No pending goal found.',ephemeral=True)

    @bot.tree.command(name='milestone_complete_review',description='Admin: approve or reject a milestone completion',guild=guild)
    @app_commands.choices(decision=[app_commands.Choice(name='Approve +30 XP',value='approved'),app_commands.Choice(name='Reject',value='rejected')])
    async def milestone_complete_review(i:discord.Interaction,member:discord.Member,chapter:app_commands.Range[int,1,3],decision:app_commands.Choice[str]):
        if not admin(i):
            return await i.response.send_message('Admins only.',ephemeral=True)
        async with get_pool().acquire() as c:
            row=await c.fetchrow("""UPDATE personal_milestones SET completion_status=$3,
                completion_reviewed_at=now(),completion_reviewer_id=$4
                WHERE discord_user_id=$1 AND chapter=$2 AND goal_status='approved'
                AND completion_status='pending' RETURNING chapter""",member.id,chapter,decision.value,i.user.id)
        if row and decision.value=='approved':
            from fitness_quest import achievements
            unlocked = await achievements.refresh(i.client,get_pool(),member.id)
            await workouts.announce_xp(i.client, get_pool(), member.id, 30,
                                       f'Milestone completed: Chapter {chapter}', unlocked)
        await i.response.send_message(('Completion approved; +30 XP awarded.' if decision.value=='approved' else 'Completion rejected; challenger may resubmit.') if row else 'No pending completion found.',ephemeral=True)
