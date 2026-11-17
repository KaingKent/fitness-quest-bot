
"""Phase 3C: resumable onboarding and button-based milestone approvals."""
import os
import discord
import phase2
import phase3

APPROVAL_CHANNEL_ID=int(os.getenv('APPROVAL_CHANNEL_ID') or 0)
ORGANIZER_ID=int(os.getenv('ORGANIZER_DISCORD_ID') or 0)

def admin(i):
    return (ORGANIZER_ID and i.user.id==ORGANIZER_ID) or i.user.guild_permissions.manage_guild

async def onboarding_step(pool,uid):
    async with pool.acquire() as c:
        name=await c.fetchval('SELECT challenger_name FROM players WHERE discord_user_id=$1',uid)
        if not name: return 'name'
        plan=await c.fetchval('SELECT 1 FROM plan_requests WHERE discord_user_id=$1 LIMIT 1',uid)
        if not plan: return 'plan'
        milestones=await c.fetchval('SELECT count(*) FROM personal_milestones WHERE discord_user_id=$1',uid)
        if milestones<3: return 'milestones'
    return 'complete'

async def start_onboarding(i,pool,bot):
    step=await onboarding_step(pool,i.user.id)
    if step=='name':
        return await i.response.send_modal(phase2.RegistrationModal(pool,bot))
    if step=='plan':
        return await i.response.send_modal(phase2.PlanModal(pool,bot))
    if step=='milestones':
        import phase3b
        return await i.response.send_modal(phase3b.SetMilestones(pool))
    await i.response.send_message('✅ Your adventure is already underway! Use **My Progress** to view your character.',ephemeral=True)

class RejectReason(discord.ui.Modal,title='Reject milestone'):
    reason=discord.ui.TextInput(label='Reason for rejection',style=discord.TextStyle.paragraph,max_length=400)
    def __init__(self,pool,uid,chapter,kind,message):
        super().__init__()
        self.pool,self.uid,self.chapter,self.kind,self.message=pool,uid,chapter,kind,message
    async def on_submit(self,i):
        if not admin(i):
            return await i.response.send_message('Admins only.',ephemeral=True)
        column='goal_status' if self.kind=='goal' else 'completion_status'
        async with self.pool.acquire() as c:
            async with c.transaction():
                row=await c.fetchrow(f"""UPDATE personal_milestones SET {column}='rejected',
                    {('goal_reviewer_id' if self.kind=='goal' else 'completion_reviewer_id')}=$3,
                    {('goal_reviewed_at' if self.kind=='goal' else 'completion_reviewed_at')}=now()
                    WHERE discord_user_id=$1 AND chapter=$2 AND {column}='pending'
                    RETURNING goal""",self.uid,self.chapter,i.user.id)
                if row:
                    await c.execute("""INSERT INTO milestone_rejections(discord_user_id,chapter,kind,reason,reviewer_id)
                        VALUES($1,$2,$3,$4,$5)""",self.uid,self.chapter,self.kind,str(self.reason),i.user.id)
        if not row:
            return await i.response.send_message('Already reviewed or no longer pending.',ephemeral=True)
        await self.message.edit(embed=reviewed_embed(self.message.embeds[0],'Rejected',i.user.display_name,str(self.reason)),view=None)
        await i.response.send_message('Rejected. The challenger can see the reason with /milestones.',ephemeral=True)

def reviewed_embed(original,status,reviewer,reason=None):
    e=original.copy()
    e.color=discord.Color.green() if status=='Approved' else discord.Color.red()
    e.add_field(name='Decision',value=f'{status} by {discord.utils.escape_markdown(reviewer)}'+(f'\\nReason: {discord.utils.escape_markdown(reason)}' if reason else ''),inline=False)
    return e

class MilestoneReviewView(discord.ui.View):
    def __init__(self,pool,uid,chapter,kind):
        super().__init__(timeout=None)
        self.pool,self.uid,self.chapter,self.kind=pool,uid,chapter,kind
        key=f'fq:mreview:{kind}:{uid}:{chapter}'
        approve=discord.ui.Button(label='Approve'+(' +30 XP' if kind=='completion' else ''),emoji='✅',style=discord.ButtonStyle.success,custom_id=key+':yes')
        reject=discord.ui.Button(label='Reject',emoji='❌',style=discord.ButtonStyle.danger,custom_id=key+':no')
        approve.callback=self.approve
        reject.callback=self.reject
        self.add_item(approve);self.add_item(reject)
    async def approve(self,i):
        if not admin(i):
            return await i.response.send_message('Admins only.',ephemeral=True)
        column='goal_status' if self.kind=='goal' else 'completion_status'
        async with self.pool.acquire() as c:
            row=await c.fetchrow(f"""UPDATE personal_milestones SET {column}='approved',
                {('goal_reviewer_id' if self.kind=='goal' else 'completion_reviewer_id')}=$3,
                {('goal_reviewed_at' if self.kind=='goal' else 'completion_reviewed_at')}=now()
                WHERE discord_user_id=$1 AND chapter=$2 AND {column}='pending'
                RETURNING chapter""",self.uid,self.chapter,i.user.id)
        if not row:
            return await i.response.send_message('Already reviewed or no longer pending.',ephemeral=True)
        await i.response.edit_message(embed=reviewed_embed(i.message.embeds[0],'Approved',i.user.display_name),view=None)
    async def reject(self,i):
        if not admin(i):
            return await i.response.send_message('Admins only.',ephemeral=True)
        await i.response.send_modal(RejectReason(self.pool,self.uid,self.chapter,self.kind,i.message))

async def post_milestone_review(bot,pool,uid,chapter,kind):
    if not APPROVAL_CHANNEL_ID: raise RuntimeError('APPROVAL_CHANNEL_ID is missing')
    channel=bot.get_channel(APPROVAL_CHANNEL_ID)
    if channel is None: channel=await bot.fetch_channel(APPROVAL_CHANNEL_ID)
    async with pool.acquire() as c:
        row=await c.fetchrow('SELECT goal,completion_note,proof_message_url FROM personal_milestones WHERE discord_user_id=$1 AND chapter=$2',uid,chapter)
        name=await c.fetchval('SELECT COALESCE(challenger_name,display_name) FROM players WHERE discord_user_id=$1',uid)
    if not row: return
    e=discord.Embed(title=('🏁 Milestone Goal Approval' if kind=='goal' else '🏆 Milestone Completion Approval'),color=discord.Color.blurple())
    e.add_field(name='Challenger',value=discord.utils.escape_markdown(name or str(uid)),inline=False)
    e.add_field(name='Chapter',value=str(chapter),inline=True)
    e.add_field(name='Goal',value=discord.utils.escape_markdown(row['goal']),inline=False)
    if kind=='completion':
        e.add_field(name='Proof details',value=discord.utils.escape_markdown(row['completion_note'] or '(none)')[:1000],inline=False)
        if row['proof_message_url']: e.add_field(name='Screenshot',value=row['proof_message_url'],inline=False)
    view=MilestoneReviewView(pool,uid,chapter,kind)
    return await channel.send(embed=e,view=view,allowed_mentions=discord.AllowedMentions.none())

async def restore_reviews(bot,pool):
    async with pool.acquire() as c:
        rows=await c.fetch("""SELECT discord_user_id,chapter,'goal' AS kind FROM personal_milestones WHERE goal_status='pending'
            UNION ALL SELECT discord_user_id,chapter,'completion' AS kind FROM personal_milestones WHERE completion_status='pending'""")
    for row in rows:
        bot.add_view(MilestoneReviewView(pool,row['discord_user_id'],row['chapter'],row['kind']))
