
import datetime as dt
import logging
import os
import discord
import phase2

log=logging.getLogger('fitness-quest.phase4')
CHANNEL_ID=int(os.getenv('ACHIEVEMENTS_CHANNEL_ID') or 0)
ACHIEVEMENTS=[
 ('first_steps','🌱','First Steps','Log your first workout.'),
 ('on_a_roll','🔥','On a Roll','Complete your workout goal in 3 different weeks.'),
 ('unbreakable','🛡️','Unbreakable','Complete your workout goal in 8 different weeks.'),
 ('creative_spark','🎨','Creative Spark','Complete 8 mini quests.'),
 ('main_character','🎭','Main Character','Complete 6 main quests.'),
 ('party_member','🤝','Party Member','Complete 2 team quests.'),
 ('pathfinder','🧭','Pathfinder','Get your first milestone approved as completed.'),
 ('quest_complete','🏁','Quest Complete','Get all 3 milestone completions approved.'),
 ('hidden_in_plain_sight','🔍','Hidden in Plain Sight','Complete all 4 mini quests in one week.'),
 ('the_unexpected','🎁','The Unexpected','Complete a main quest in each of the 3 chapters.'),
 ('weekly_warrior','💪','Weekly Warrior','Meet your weekly workout target for the first time.'),
 ('perfect_pace','⚡','Perfect Pace','Meet your weekly target for 4 consecutive weeks.'),
 ('chungus_consistency','👑','Consistency Champion','Meet your weekly target in all 12 challenge weeks.'),
 ('workout_wonder','🏋️','Workout Wonder','Log 25 workouts.'),
 ('quest_quencher','🗺️','Quest Quencher','Complete 20 quests.'),
 ('peak_cinema','🎯','Main Quest Master','Complete all 12 weekly main quests.'),
 ('reflective_resolve','📖','Reflective Resolve','Submit 4 weekly reflections.'),
 ('performative_journaling','📚','Reflection Royalty','Submit all 12 weekly reflections.'),
 ('no_crumbs_left','🌄','Chapter Champion','Complete every mini and main quest in one 4-week chapter.'),
 ('chungus_completionist','🌟','Chungus Completionist','Unlock the other 19 achievements.')
]
BY_KEY={a[0]:a for a in ACHIEVEMENTS}
HIDDEN_ACHIEVEMENTS={'hidden_in_plain_sight','the_unexpected'}

async def snapshot(pool,uid):
    async with pool.acquire() as c:
        workouts=await c.fetch('SELECT date_trunc(\'week\',workout_date::timestamp)::date AS week_start,count(*)::int AS n FROM workouts WHERE discord_user_id=$1 GROUP BY 1',uid)
        history=await c.fetch('SELECT effective_date,weekly_target FROM plan_history WHERE discord_user_id=$1 ORDER BY effective_date',uid)
        quests=await c.fetch('SELECT q.kind,q.week_start,q.is_team FROM quest_submissions s JOIN quests q ON q.id=s.quest_id WHERE s.discord_user_id=$1 AND s.status=\'accepted\'',uid)
        milestones=await c.fetchval("SELECT count(*) FROM personal_milestones WHERE discord_user_id=$1 AND completion_status='approved'",uid)
        unlocked=await c.fetch('SELECT achievement_key FROM achievement_unlocks WHERE discord_user_id=$1',uid)
        reflections=await c.fetch('SELECT week_start FROM weekly_reflections WHERE discord_user_id=$1',uid)
        name=await c.fetchval('SELECT COALESCE(challenger_name,display_name) FROM players WHERE discord_user_id=$1',uid)
    success_weeks=set()
    successful=0
    for row in workouts:
        eligible=[h for h in history if h['effective_date']<=row['week_start']]
        if eligible and eligible[-1]['weekly_target']>0 and row['n']>=eligible[-1]['weekly_target']:
            successful+=1
            success_weeks.add(row['week_start'])
    minis=[q for q in quests if q['kind']=='mini']
    mains=[q for q in quests if q['kind']=='main']
    mini_weeks={}
    for q in minis: mini_weeks[q['week_start']]=mini_weeks.get(q['week_start'],0)+1
    start_raw=os.getenv('FITNESS_QUEST_SEASON_START','')
    start=dt.date.fromisoformat(start_raw) if start_raw else None
    chapters=set()
    if start:
        for q in mains:
            w=(q['week_start']-start).days//7
            if 0<=w<12: chapters.add(w//4+1)
    streak=0
    best_streak=0
    for w in sorted(success_weeks):
        streak=streak+1 if w-dt.timedelta(days=7) in success_weeks else 1
        best_streak=max(best_streak,streak)
    quest_weeks={}
    for q in quests:
        if start and 0 <= (q['week_start']-start).days < 84:
            chapter=(q['week_start']-start).days//28
            key=(chapter,q['week_start'])
            quest_weeks.setdefault(key,{'mini':0,'main':0})[q['kind']]+=1
    perfect_chapter=any(all(quest_weeks.get((chapter,start+dt.timedelta(days=7*(chapter*4+i))),{}).get('mini',0)>=4 and quest_weeks.get((chapter,start+dt.timedelta(days=7*(chapter*4+i))),{}).get('main',0)>=1 for i in range(4)) for chapter in range(3)) if start else False
    season_weeks={start+dt.timedelta(days=7*i) for i in range(12)} if start else set()
    satisfied={
      'first_steps':bool(workouts),
      'on_a_roll':successful>=3,
      'unbreakable':successful>=8,
      'creative_spark':len(minis)>=8,
      'main_character':len(mains)>=6,
      'party_member':sum(bool(q['is_team']) for q in quests)>=2,
      'pathfinder':milestones>=1,
      'quest_complete':milestones>=3,
      'hidden_in_plain_sight':any(n>=4 for n in mini_weeks.values()),
      'the_unexpected':len(chapters)==3,
      'weekly_warrior':successful>=1,
      'perfect_pace':best_streak>=4,
      'chungus_consistency':bool(start) and season_weeks.issubset(success_weeks),
      'workout_wonder':sum(r['n'] for r in workouts)>=25,
      'quest_quencher':len(quests)>=20,
      'peak_cinema':bool(start) and all(any(q['week_start']==start+dt.timedelta(days=7*i) for q in mains) for i in range(12)),
      'reflective_resolve':len(reflections)>=4,
      'performative_journaling':bool(start) and season_weeks.issubset({r['week_start'] for r in reflections}),
      'no_crumbs_left':perfect_chapter,
      'chungus_completionist':False
    }
    week=phase2.monday(phase2.today())
    return dict(satisfied=satisfied,unlocked={r['achievement_key'] for r in unlocked},
                mini_week=mini_weeks.get(week,0),main_week=sum(q['week_start']==week for q in mains),
                milestones=int(milestones or 0),name=name)

async def refresh(bot,pool,uid):
    data=await snapshot(pool,uid)
    new=[]
    async with pool.acquire() as c:
        for key,yes in data['satisfied'].items():
            if yes:
                result=await c.execute('INSERT INTO achievement_unlocks(discord_user_id,achievement_key) VALUES($1,$2) ON CONFLICT DO NOTHING',uid,key)
                if result=='INSERT 0 1': new.append(key)
    # Completionist depends on the other 19 and must be checked after their inserts.
    async with pool.acquire() as c:
        keys={r['achievement_key'] for r in await c.fetch('SELECT achievement_key FROM achievement_unlocks WHERE discord_user_id=$1',uid)}
        if all(key in keys for key in BY_KEY if key!='chungus_completionist'):
            result=await c.execute('INSERT INTO achievement_unlocks(discord_user_id,achievement_key) VALUES($1,$2) ON CONFLICT DO NOTHING',uid,'chungus_completionist')
            if result=='INSERT 0 1': new.append('chungus_completionist')
    if new and CHANNEL_ID and bot:
        channel=bot.get_channel(CHANNEL_ID)
        if channel is None:
            try: channel=await bot.fetch_channel(CHANNEL_ID)
            except discord.HTTPException: channel=None
        if channel:
            for key in new:
                _,emoji,title,description=BY_KEY[key]
                e=discord.Embed(title=f'{emoji} Achievement Unlocked: {title}',
                                description=f'**{discord.utils.escape_markdown(data["name"] or str(uid))}**\n{description}\n**+15 XP**',
                                color=discord.Color.gold())
                try: await channel.send(embed=e,allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException: log.exception('Achievement announcement failed: %s/%s',uid,key)
    return new

async def xp(pool,uid):
    async with pool.acquire() as c:
        return int(await c.fetchval('SELECT count(*)*15 FROM achievement_unlocks WHERE discord_user_id=$1',uid) or 0)

async def xp_week(pool,uid,week):
    async with pool.acquire() as c:
        return int(await c.fetchval("SELECT count(*)*15 FROM achievement_unlocks WHERE discord_user_id=$1 AND unlocked_at >= $2::date AND unlocked_at < ($2::date + interval '7 days')",uid,week) or 0)

async def profile_stats(pool,uid):
    s=await snapshot(pool,uid)
    return dict(mini=s['mini_week'],main=s['main_week'],milestones=s['milestones'],achievements=len(s['unlocked']))

def register(bot,get_pool):
    guild=discord.Object(id=int(os.environ['DISCORD_GUILD_ID']))
    @bot.tree.command(name='achievement_backfill',description='Admin: recognize past activity without announcements',guild=guild)
    async def achievement_backfill(i:discord.Interaction):
        if not i.user.guild_permissions.manage_guild:
            return await i.response.send_message('Manage Server permission required.',ephemeral=True)
        await i.response.defer(ephemeral=True)
        async with get_pool().acquire() as c:
            ids=await c.fetch('SELECT discord_user_id FROM players')
        total=0
        for row in ids:
            total+=len(await refresh(None,get_pool(),row['discord_user_id']))
        await i.followup.send(f'Backfill complete: {total} previously earned achievements recorded. No historical announcements posted.',ephemeral=True)

    @bot.tree.command(name='achievements',description='See your Fitness Quest achievements',guild=guild)
    async def achievements(i:discord.Interaction,member:discord.Member|None=None):
        uid=member.id if member else i.user.id
        await i.response.defer(ephemeral=True)
        if member is None: await refresh(i.client,get_pool(),uid)
        s=await snapshot(get_pool(),uid)
        e=discord.Embed(title='🏆 Fitness Quest Achievements',description=f'{len(s["unlocked"])}/{len(ACHIEVEMENTS)} unlocked · {len(s["unlocked"])*15}/{len(ACHIEVEMENTS)*15} XP',color=discord.Color.gold())
        for key,emoji,title,description in ACHIEVEMENTS:
            unlocked=key in s['unlocked']
            if key in HIDDEN_ACHIEVEMENTS and not unlocked:
                emoji,title,description='❔','???','Secret achievement. Unlock it to reveal the requirement.'
            e.add_field(name=f'{emoji} {title} '+('✅' if unlocked else '🔒'),value=description,inline=False)
        await i.followup.send(embed=e,ephemeral=True)

async def total_xp(pool,uid):
    workout=await phase2.workout_total(pool,uid)
    async with pool.acquire() as c:
        extra=await c.fetchval('''SELECT COALESCE(SUM(xp),0)::int FROM (
            SELECT q.xp FROM quest_submissions s JOIN quests q ON q.id=s.quest_id
            WHERE s.discord_user_id=$1 AND s.status='accepted'
            UNION ALL SELECT 10 FROM weekly_reflections WHERE discord_user_id=$1
            UNION ALL SELECT 30 FROM personal_milestones
              WHERE discord_user_id=$1 AND completion_status='approved'
            UNION ALL SELECT 15 FROM achievement_unlocks WHERE discord_user_id=$1
        ) x''',uid)
    return workout+int(extra or 0)
