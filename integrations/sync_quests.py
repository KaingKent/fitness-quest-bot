#!/usr/bin/env python3
"""Safely reconcile the organizer's Google Sheet with Supabase quests.

Default: preview only. --apply writes drafts after explicit review.
Uses a Google service account shared on the organizer's spreadsheet.
"""
import argparse
import asyncio
import datetime as dt
import os
from pathlib import Path

import asyncpg
import instance_config
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession



SHEET_ID = os.getenv('QUEST_SHEET_ID', '1fqjWGo2LOoK8j_YPPSRmB6F96anw-ouS43i3whIvmpc')
SCOPE = ['https://www.googleapis.com/auth/spreadsheets']


def sheet_client():
    path = os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE', '').strip()
    if not path or not Path(path).is_file():
        raise RuntimeError('Set GOOGLE_SERVICE_ACCOUNT_FILE to a service-account JSON key on EC2')
    creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPE)
    return AuthorizedSession(creds)


def sheets_call(session, method, path, payload=None, params=None):
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/{path}'
    response = session.request(method, url, json=payload, params=params, timeout=30)
    if not response.ok:
        raise RuntimeError(f'Google Sheets {response.status_code}: {response.text[:700]}')
    return response.json()


def parse_sheet(session, weeks):
    raw = sheets_call(session, 'GET', 'values/\'Quest Planner\'!A1:J61', params={'valueRenderOption':'FORMATTED_VALUE'})
    rows = raw.get('values', [])
    expected = ['Week','Week Start','Quest Type','Title','Instructions','Proof Type','Team Quest','Quest ID','Sync Status','Last Synced']
    if not rows or rows[0][:len(expected)] != expected:
        raise ValueError('Quest Planner columns changed; expected A:J headers. Refusing to sync.')
    result = []
    errors = []
    for idx, r in enumerate(rows[1:], start=2):
        r = (r + ['']*10)[:10]
        if not str(r[0]).strip():
            continue
        try:
            week = int(r[0]); day = dt.date.fromisoformat(r[1].strip())
        except (ValueError, TypeError):
            errors.append(f'Row {idx}: invalid week or date'); continue
        if week not in weeks: continue
        kind = {'mini':'mini','main':'main'}.get(r[2].strip().lower())
        proof = {'text':'text','screenshot':'image','image':'image','either':'either'}.get(r[5].strip().lower())
        team = {'yes':True,'no':False}.get(r[6].strip().lower())
        title, instructions = r[3].strip(), r[4].strip()
        if not kind or not proof or team is None or not title or not instructions:
            errors.append(f'Row {idx}: missing/invalid quest details');continue
        if len(title)>100 or len(instructions)>1500:
            errors.append(f'Row {idx}: title >100 or instructions >1500 characters');continue
        try: quest_id = int(r[7]) if str(r[7]).strip() else None
        except ValueError:
            errors.append(f'Row {idx}: invalid Quest ID');continue
        result.append(dict(row=idx,week=week,day=day,kind=kind,title=title,instructions=instructions,proof=proof,team=team,quest_id=quest_id,xp=5 if kind=='mini' else 20))
    if errors: raise ValueError('\n'.join(errors))
    for w in weeks:
        selected=[q for q in result if q['week']==w]
        if len([q for q in selected if q['kind']=='mini'])!=4 or len([q for q in selected if q['kind']=='main'])!=1:
            raise ValueError(f'Week {w} must have exactly 4 mini and 1 main quest (found {len(selected)} total)')
        dates={q['day'] for q in selected}
        if len(dates)!=1 or next(iter(dates)).weekday()!=0:
            raise ValueError(f'Week {w} has inconsistent or non-Monday start dates')
    return result


def plan(rows, existing):
    by_id={q['id']:q for q in existing}
    used=set(); actions=[]
    for row in rows:
        matches=[]
        if row['quest_id']:
            db=by_id.get(row['quest_id'])
            if not db: raise ValueError(f"Row {row['row']}: Quest ID {row['quest_id']} not in database")
            if db['week_start']!=row['day'] or db['kind']!=row['kind']:
                raise ValueError(f"Row {row['row']}: Quest ID belongs to a different week/type")
            matches=[db]
        else:
            # Exact normalized title match within same week and kind. Never guess by slot/order.
            matches=[q for q in existing if q['week_start']==row['day'] and q['kind']==row['kind'] and q['title'].strip().casefold()==row['title'].casefold()]
        if len(matches)>1: raise ValueError(f"Row {row['row']}: multiple database quests match title; manually resolve")
        db=matches[0] if matches else None
        if db and db['id'] in used: raise ValueError(f"Database quest #{db['id']} matched multiple spreadsheet rows")
        if db: used.add(db['id'])
        if db and (db['published'] or db['release_date'] is not None or db['announcement_message_id'] is not None):
            raise ValueError(f"Row {row['row']}: quest #{db['id']} is scheduled or published; refusing to modify")
        if not db:
            # If a quest was created via Discord with a different title, do not accidentally add a sixth.
            same=[q for q in existing if q['week_start']==row['day'] and q['kind']==row['kind']]
            if len(same)>= (4 if row['kind']=='mini' else 1):
                raise ValueError(f"Row {row['row']}: {row['kind']} slots full in database; existing Discord quest may have a different title. Resolve before importing")
        actions.append((row,db,'UPDATE' if db else 'CREATE'))
    return actions


def update_sheet(session, actions):
    now=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    data=[]
    for row,db,action in actions:
        data.append({'range':f"'Quest Planner'!H{row['row']}:J{row['row']}", 'values':[[str(db['id']), 'Synced', now]]})
    if data:
        sheets_call(session,'POST','values:batchUpdate',{'valueInputOption':'RAW','data':data})


async def run(args):
    session=sheet_client()
    rows=parse_sheet(session, set(args.weeks))
    url=os.environ.get('DATABASE_URL')
    if not url: raise RuntimeError('DATABASE_URL missing from bot .env')
    conn=await asyncpg.connect(url, ssl='require')
    try:
        start=min(q['day'] for q in rows); end=max(q['day'] for q in rows)
        existing=await conn.fetch('''SELECT id,week_start,kind,title,instructions,xp,proof_type,is_team,
            published,release_date,announcement_message_id FROM quests
            WHERE week_start BETWEEN $1 AND $2 ORDER BY id''',start,end)
        actions=plan(rows,existing)
        for row,db,action in actions:
            print(f"{action:6} row {row['row']:2} week {row['week']} {row['kind']:4} | {row['title']}" + (f" [quest #{db['id']}]" if db else ''))
        if not args.apply:
            print('\nPREVIEW ONLY. No database or sheet changes. Rerun with --apply after checking matches.')
            return
        # Advisory lock prevents concurrent imports; transaction rolls back on error.
        async with conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock($1)', 20261130)
            # Re-read under lock and verify no quest has been scheduled since preview.
            current=await conn.fetch('''SELECT id,week_start,kind,title,instructions,xp,proof_type,is_team,
                published,release_date,announcement_message_id FROM quests
                WHERE week_start BETWEEN $1 AND $2 ORDER BY id FOR UPDATE''',start,end)
            actions=plan(rows,current)
            for row,db,action in actions:
                number=row['week'];chapter=(number-1)//4+1
                await conn.execute('''INSERT INTO quest_weeks(week_start,chapter,week_number) VALUES($1,$2,$3)
                    ON CONFLICT(week_start) DO UPDATE SET chapter=EXCLUDED.chapter,week_number=EXCLUDED.week_number''',row['day'],chapter,number)
                if db:
                    await conn.execute('''UPDATE quests SET title=$2,instructions=$3,xp=$4,proof_type=$5,is_team=$6
                        WHERE id=$1''',db['id'],row['title'],row['instructions'],row['xp'],row['proof'],row['team'])
                else:
                    record=await conn.fetchrow('''INSERT INTO quests(week_start,kind,title,instructions,xp,proof_type,is_team)
                        VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id''',row['day'],row['kind'],row['title'],row['instructions'],row['xp'],row['proof'],row['team'])
                    db={'id':record['id']}
                row['saved_id']=db['id']
        update_sheet(session,[(row,{'id':row['saved_id']},action) for row,db,action in actions])
        print('Imported drafts and wrote Quest IDs to sheet. No quests published.')
    finally:
        await conn.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Preview or import quest drafts from Google Sheets')
    parser.add_argument('--weeks',nargs='+',type=int,default=[1,2],help='Week numbers to sync (default 1 2)')
    parser.add_argument('--apply',action='store_true',help='Commit changes after reviewing preview')
    a=parser.parse_args()
    try: asyncio.run(run(a))
    except Exception as exc: raise SystemExit(f'ERROR: {exc}')
