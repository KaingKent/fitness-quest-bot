bot treats workouts like xp make workout fun
## Features

### Challenger Dashboard
- Interactive button-based dashboard.
- Easy registration and character creation.
- Personalized challenger display names.
- Access to all major features without memorizing slash commands.
- Persistent dashboard buttons that survive bot restarts.

### Fitness Tracking
- Personalized weekly workout goals.
- Administrator-approved fitness plans.
- Structured workout logging with workout type, title, duration, date, notes, and photo evidence.
- Automatic workout history tracking.
- Individual adventurer journal channels.
- Automatic workout posts in personal journals.
- Normalized fitness XP based on individual workout goals.
- Weekly workout progress tracking.

### Quest System
- 12-week seasonal quest calendar.
- Four mini quests per week.
- One main quest per week.
- Randomized, secret mini-quest release dates.
- Automatic quest scheduling and publication.
- Automatic Discord quest announcements.
- Text and image-based quest submissions.
- Quest-specific proof requirements.
- Automatic submission deadlines.
- Administrator submission review.
- Protection against premature quest publication.
- Persistent quest schedules across bot restarts.

### Character Progression
- RPG-inspired XP and leveling system.
- 15 character levels, from Chud to Big Chungus.
- Automatic level progression.
- Collectible Discord level roles.
- Previously unlocked roles remain collectible.
- Automatic level-up announcements.
- Personalized character profile cards.
- Visual XP and workout progress bars.

### Achievements
- 20 unlockable achievements.
- Hidden achievements.
- Achievement-based XP rewards.
- Automatic achievement tracking.
- Achievement progress and completion tracking.
- Discord achievement announcements.

### Personal Milestones
- Three personalized milestones per challenger.
- Administrator-approved milestone goals.
- Milestone completion submissions.
- XP rewards for completed milestones.
- Individual milestone progress tracking.

### Weekly Reflections
- Optional weekly reflection submissions.
- XP rewards for completing reflections.
- Private reflections accessible to the challenger and administrator.
- Weekly reflection history.

### Leaderboards
- Overall XP rankings.
- Challenger level and progression comparisons.
- Automatic leaderboard updates.
- Mobile-friendly leaderboard presentation.

### Administration
- Automated Discord server setup.
- Automatic channel and category creation.
- Automatic level role creation.
- Fitness plan approval system.
- Quest and milestone submission management.
- Private administration channels.
- Automatic personal journal creation.
- Season start restrictions.
- Configurable season start dates.
- Read-only quest calendar auditing.
- Reduced manual administration through automated progression tracking.

### Google Sheets Integration
- Spreadsheet-based quest planning.
- Automatic quest draft imports.
- Automatic database-specific Quest ID assignment.
- Participant reporting.
- Workout log reporting.
- Quest submission reporting.
- Leaderboard reporting.
- Achievement reporting.
- Weekly reflection reporting.
- Milestone reporting.
- Scheduled synchronization every 15 minutes.
- Preview mode before applying synchronization changes.

### Multi-Server Support
- Single shared codebase supporting multiple independent Discord bots.
- Separate Discord applications and bot identities.
- Independent Supabase databases.
- Independent Google Sheets integrations.
- Instance-specific environment configurations.
- Independent quest schedules and character progression.
- Independent synchronization jobs and logs.
- Support for different season start dates.
- Centralized code maintenance and updates.

### Infrastructure and Reliability
- AWS EC2 hosting.
- Python and discord.py.
- Supabase PostgreSQL database.
- Asynchronous database connection pooling.
- Shared Python virtual environment.
- Independent systemd services.
- Persistent database-backed progression.
- Automatic recovery of persistent Discord interfaces.
- Duplicate quest announcement recovery.
- Secure instance configuration files.
- Git-based version control.
- Independent background synchronization jobs.

## Technology Stack

| Component | Technology |
|---|---|
| Programming Language | Python |
| Discord Framework | discord.py |
| Database | Supabase / PostgreSQL |
| Database Driver | asyncpg |
| Reporting and Quest Planning | Google Sheets API |
| Hosting | AWS EC2 |
| Operating System | Ubuntu |
| Service Management | systemd |
| Scheduled Tasks | cron |
| Version Control | Git / GitHub |

## Planned Features

- Cross-server shared characters.
- Shared fitness progress across participating Discord servers.
- Cross-server quest completion synchronization.
- Shared XP, levels, achievements, and milestones.
- Synchronized quest release schedules for linked servers.
- Administrator-managed character linking.
- Duplicate XP prevention across linked servers.
- Independent server leaderboards with shared character progression.
