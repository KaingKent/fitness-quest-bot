#!/usr/bin/env python3
"""Read-only Supabase reporting export to existing Google Sheets tabs.

Default is preview; --apply overwrites only A:D / A:E reporting data, not planner tabs.
"""

# Load environment variables from the project root.
from pathlib import Path as _EnvPath
import instance_config


import argparse
import asyncio
import datetime as dt
import os
from pathlib import Path

import asyncpg

from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession


SHEET_ID=os.getenv('QUEST_SHEET_ID')
SCOPE=['https://www.googleapis.com/auth/spreadsheets']
TABS={
 'Participants':['Name','XP','Workouts','Last Updated'],
 'Workout Logs':['Submission ID','Name','Date','Status','XP'],
 'Quest Submissions':['Submission ID','Quest ID','Name','Status','XP'],
 'Leaderboard':['Rank','Name','Total XP'],
 'Achievements':['Name','Achievement','Unlocked At'],
 'Weekly Reflections':['Name','Week Start','Completed','XP'],
 'Milestones':['Name','Milestone','Status','XP'],
}

def sheet_client():
    path=os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE','')
    if not SHEET_ID or not path or not Path(path).is_file():
        raise RuntimeError('Set QUEST_SHEET_ID and GOOGLE_SERVICE_ACCOUNT_FILE in .env')
    return AuthorizedSession(service_account.Credentials.from_service_account_file(path,scopes=SCOPE))

def request(session, method, path, payload=None):
    url=f'https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/{path}'
    r=session.request(method,url,json=payload,timeout=45)
    if not r.ok: raise RuntimeError(f'Google Sheets {r.status_code}: {r.text[:500]}')
    return r.json()

def display(v):
    if v is None:return ''
    if isinstance(v,(dt.date,dt.datetime)):return v.isoformat()
    return str(v)

def safe_cell(v):
    # USER_ENTERED is not used, but prefix spreadsheet formulas defensively.
    s=display(v)
    return "'"+s if s.startswith(('=','+','-','@')) else s

async def read_reports():
    con=await asyncpg.connect(os.environ['DATABASE_URL'])
    try:
        async with con.transaction(isolation='repeatable_read',readonly=True):
            players=await con.fetch('SELECT discord_user_id, COALESCE(challenger_name,display_name) AS name FROM players ORDER BY name')
            workouts=await con.fetch('SELECT id,discord_user_id,workout_date FROM workouts ORDER BY workout_date,id')
            plans=await con.fetch('SELECT discord_user_id,weekly_target,effective_date FROM plan_history ORDER BY effective_date')
            quests=await con.fetch('SELECT s.id,s.discord_user_id,s.quest_id,s.status,q.xp FROM quest_submissions s JOIN quests q ON q.id=s.quest_id ORDER BY s.id')
            reflections=await con.fetch('SELECT discord_user_id,week_start FROM weekly_reflections ORDER BY week_start,discord_user_id')
            milestones=await con.fetch('SELECT discord_user_id,chapter,goal,completion_status FROM personal_milestones ORDER BY discord_user_id,chapter')
            achievements=await con.fetch('SELECT discord_user_id,achievement_key,unlocked_at FROM achievement_unlocks ORDER BY unlocked_at')
    finally:await con.close()
    names={r['discord_user_id']:r['name'] or 'Unknown' for r in players}
    counts={uid:0 for uid in names}; weeks={}; targets={}
    for p in plans:targets.setdefault(p['discord_user_id'],[]).append(p)
    for w in workouts:
        uid=w['discord_user_id'];counts[uid]=counts.get(uid,0)+1
        monday=w['workout_date']-dt.timedelta(days=w['workout_date'].weekday())
        weeks[(uid,monday)]=weeks.get((uid,monday),0)+1
    workout_xp={uid:0 for uid in names}; per_workout={}
    for (uid,week),n in weeks.items():
        active=[p for p in targets.get(uid,[]) if p['effective_date']<=week]
        if not active:continue
        target=active[-1]['weekly_target']
        if not target:continue
        def xp(c):
            done=min(c,target)
            return (60*done+target)//(2*target)+(20 if done==target else 0)
        workout_xp[uid]=workout_xp.get(uid,0)+xp(n)
        per_workout[(uid,week)]=[xp(i)-xp(i-1) for i in range(1,n+1)]
    total={uid:workout_xp.get(uid,0) for uid in names}
    for q in quests:
        if q['status']=='accepted':total[q['discord_user_id']]=total.get(q['discord_user_id'],0)+q['xp']
    for r in reflections:total[r['discord_user_id']]=total.get(r['discord_user_id'],0)+10
    for m in milestones:
        if m['completion_status']=='approved':total[m['discord_user_id']]=total.get(m['discord_user_id'],0)+30
    for a in achievements:total[a['discord_user_id']]=total.get(a['discord_user_id'],0)+15
    now=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    data={k:[] for k in TABS}
    for uid,name in sorted(names.items(),key=lambda p:p[1].casefold()):
        data['Participants'].append([name,total.get(uid,0),counts.get(uid,0),now])
    ranked=sorted(names.items(),key=lambda p:(-total.get(p[0],0),p[1].casefold()))
    for i,(uid,name) in enumerate(ranked,1):data['Leaderboard'].append([i,name,total.get(uid,0)])
    used={}
    for w in workouts:
        uid=w['discord_user_id'];week=w['workout_date']-dt.timedelta(days=w['workout_date'].weekday())
        ix=used.get((uid,week),0);used[(uid,week)]=ix+1
        awards=per_workout.get((uid,week),[])
        data['Workout Logs'].append([str(w['id']),names.get(uid,'Unknown'),w['workout_date'],'Logged',awards[ix] if ix<len(awards) else 0])
    for q in quests:
        data['Quest Submissions'].append([q['id'],q['quest_id'],names.get(q['discord_user_id'],'Unknown'),q['status'],q['xp'] if q['status']=='accepted' else 0])
    for r in reflections:data['Weekly Reflections'].append([names.get(r['discord_user_id'],'Unknown'),r['week_start'],'Yes',10])
    for m in milestones:data['Milestones'].append([names.get(m['discord_user_id'],'Unknown'),f"Chapter {m['chapter']}: {m['goal']}",m['completion_status'],30 if m['completion_status']=='approved' else 0])
    for a in achievements:data['Achievements'].append([names.get(a['discord_user_id'],'Unknown'),a['achievement_key'].replace('_',' ').title(),a['unlocked_at']])
    return data

def write_reports(data):
    session=sheet_client()
    # Validate all seven headers BEFORE modifying any tab.
    ranges=[f"'{name}'!A1:{chr(64+len(header))}1" for name,header in TABS.items()]
    got=request(session,'GET','values:batchGet?' + '&'.join('ranges='+__import__('urllib.parse',fromlist=['quote']).quote(x,safe='') for x in ranges))
    for (name,expected),item in zip(TABS.items(),got.get('valueRanges',[])):
        if item.get('values',[[]])[0]!=expected:raise RuntimeError(f'{name} headers changed; no changes written')
    # Clear ONLY existing report cells; never clear Quest Planner or Quest Calendar.
    clear={'ranges':[f"'{name}'!A2:{chr(64+len(header))}" for name,header in TABS.items()]}
    request(session,'POST','values:batchClear',clear)
    payload={'valueInputOption':'RAW','data':[{'range':f"'{name}'!A2",'values':[[safe_cell(v) if isinstance(v,str) else display(v) if isinstance(v,(dt.date,dt.datetime)) else v for v in row] for row in data[name]]} for name in TABS if data[name]]}
    if payload['data']:request(session,'POST','values:batchUpdate',payload)

async def main():
    ap=argparse.ArgumentParser();ap.add_argument('--apply',action='store_true');args=ap.parse_args()
    data=await read_reports()
    for name,rows in data.items():print(f'{name}: {len(rows)} rows')
    if args.apply:
        await asyncio.to_thread(write_reports,data)
        print('Reporting tabs updated. Quest Planner and Quest Calendar untouched.')
    else:print('PREVIEW ONLY. Run with --apply to update reporting tabs.')

if __name__=='__main__':asyncio.run(main())
