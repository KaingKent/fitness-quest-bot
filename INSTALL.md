# Preseason quest test patch

This patch changes only `phase3.py` and only when `FITNESS_QUEST_PRESEASON_TEST=true`. It permits an organizer to create and manually publish a test quest for Monday 2026-11-16. It does not change the November 30 season start or Weeks 1/2 scheduler. Preseason quests appear in `/quests` during November 16–22; submission and approval use the existing bot paths and may grant test XP. No automated reset is included.

## Windows PowerShell
```powershell
scp -i "$env:USERPROFILE\Downloads\chungus.pem" "$env:USERPROFILE\Downloads\fitness-quest-preseason-test.zip" ubuntu@13.218.241.58:/home/ubuntu/
ssh -i "$env:USERPROFILE\Downloads\chungus.pem" ubuntu@13.218.241.58
```

## EC2 Ubuntu
```bash
cd ~/fitness-quest-bot
cp phase3.py "phase3.py.backup.$(date +%Y%m%d-%H%M%S)"
unzip -o ~/fitness-quest-preseason-test.zip -d ~/fitness-quest-bot
nano .env
```
Add `FITNESS_QUEST_PRESEASON_TEST=true`, then:
```bash
./.venv/bin/python -m py_compile phase3.py
sudo systemctl restart fitness-quest
sudo journalctl -u fitness-quest -n 60 --no-pager
```

## Discord test
1. `/quest_add` with `week_start: 2026-11-16`, type mini, and test title/instructions.
2. Copy the returned quest ID.
3. `/quest_publish` with that ID.
4. `/quests` should show the test quest. Submit proof with `/quest_submit` and test public/private and approval workflows.

## After testing
Set `FITNESS_QUEST_PRESEASON_TEST=false` in `.env` and restart. The test quest and XP remain in the test database until you deliberately reset them before launch. Do not use the production quest IDs 5–14 for testing. The patch does not announce the test quest automatically; `/quests` is the test entry point.
