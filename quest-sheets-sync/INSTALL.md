# Fitness Quest — Google Sheets quest import

This add-on does not change the Discord bot or scheduler. **Preview is the default**; `--apply` creates/updates *unpublished drafts only*. It does not assign release days or announce quests.

## 1. Google service account

1. In Google Cloud Console, create or select a project, enable **Google Sheets API**, create a **service account**, and generate a JSON key. Treat it as a password. Google may restrict key creation in some organizations.
2. Open the JSON locally and copy the service account's `client_email` (not the private key).
3. In the Fitness Quest spreadsheet, click **Share** and give that service-account email **Editor** access. Keep the sheet private from participants.
4. Transfer the JSON securely to EC2 (example below). Do **not** put the key in Google Sheets, Supabase tables, or Discord.

## 2. Upload and install on EC2

From Windows PowerShell, with `quest-sheets-sync.zip` in Downloads:

```powershell
scp -i "$env:USERPROFILE\Downloads\chungus.pem" "$env:USERPROFILE\Downloads\quest-sheets-sync.zip" ubuntu@13.218.241.58:/home/ubuntu/
scp -i "$env:USERPROFILE\Downloads\chungus.pem" "$env:USERPROFILE\Downloads\YOUR_SERVICE_ACCOUNT_KEY.json" ubuntu@13.218.241.58:/home/ubuntu/
ssh -i "$env:USERPROFILE\Downloads\chungus.pem" ubuntu@13.218.241.58
```

On EC2:

```bash
cd /home/ubuntu/fitness-quest-bot
unzip -o /home/ubuntu/quest-sheets-sync.zip -d .
mkdir -p /home/ubuntu/.config/fitness-quest
mv /home/ubuntu/YOUR_SERVICE_ACCOUNT_KEY.json /home/ubuntu/.config/fitness-quest/google-service-account.json
chmod 600 /home/ubuntu/.config/fitness-quest/google-service-account.json
./.venv/bin/pip install google-auth requests
```

Add these lines to `/home/ubuntu/fitness-quest-bot/.env` (leave `DATABASE_URL` untouched):

```dotenv
QUEST_SHEET_ID=1fqjWGo2LOoK8j_YPPSRmB6F96anw-ouS43i3whIvmpc
GOOGLE_SERVICE_ACCOUNT_FILE=/home/ubuntu/.config/fitness-quest/google-service-account.json
```

## 3. Preview, then import

```bash
cd /home/ubuntu/fitness-quest-bot
./.venv/bin/python quest-sheets-sync/sync_quests.py
```

Read all 10 lines. **If the existing Discord-created quest shows CREATE instead of UPDATE, do not apply**; the existing quest's title differs from the sheet. Ask for help matching it safely.

Only after the preview matches your expectations:

```bash
./.venv/bin/python quest-sheets-sync/sync_quests.py --apply
```

Re-run preview later to see subsequent edits. To sync other weeks use `--weeks 3 4` (requires five completed quests per selected week).

## Important limitations

- This is a **manual import command**, not continuous live sync or a Google Sheets button yet. It is safer for the first import and can be automated later.
- It updates draft content, never published or scheduled quests. It will refuse a week if an existing scheduled quest is encountered.
- It matches existing quests by Quest ID first, then exact case-insensitive title within the same week and kind. If an existing Discord quest was renamed in Sheets before its ID was linked, manually resolve it first.
- If the database transaction succeeds but updating the Google Sheet fails, the next preview can match newly created quests by title. Do not change quest titles until IDs are written.
- `Quest ID`, `Sync Status`, and `Last Synced` are filled after successful import; don't edit them yourself.
- For security, the service account should be limited to this spreadsheet and the JSON key kept off public storage.
