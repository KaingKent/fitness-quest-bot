"""Fitness Quest: public journal workout logging, approved plans, XP, profiles and Sheets sync."""
import asyncio
import datetime as dt
import logging
import os
import uuid
from zoneinfo import ZoneInfo
from pathlib import Path

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
from fitness_quest import workouts
from fitness_quest import quests
from fitness_quest import quest_reviews
from fitness_quest import quest_progress
from fitness_quest import achievements
from fitness_quest import level_roles
from fitness_quest import quest_scheduler
from fitness_quest import season_gate
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fitness-quest")
TZ = ZoneInfo("America/New_York")
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
pool: asyncpg.Pool | None = None
workouts.register(bot, lambda: pool)
quests.register(bot, lambda: pool)
quest_reviews.register(bot, lambda: pool)
achievements.register(bot, lambda: pool)
calendar_loop=quest_scheduler.register(bot,lambda: pool)


def week_bounds(day: dt.date):
    start = day - dt.timedelta(days=day.weekday())
    return start, start + dt.timedelta(days=7)


async def ensure_player(user: discord.abc.User):
    async with pool.acquire() as con:
        await con.execute("""insert into players(discord_user_id,display_name)
            values($1,$2) on conflict(discord_user_id) do update
            set display_name=excluded.display_name""", user.id, user.display_name)


async def weekly_progress(user_id: int, day: dt.date):
    count, target, xp = await workouts.get_week(pool, user_id, workouts.monday(day))
    return count, target, xp


async def publish_workout(interaction: discord.Interaction, workout_id: uuid.UUID):
    return await workouts.publish_journal(bot, pool, workout_id, interaction.user.id)


class WorkoutModal(discord.ui.Modal, title="Log your workout"):
    workout_title = discord.ui.TextInput(label="Workout title", placeholder="Push Day — Chest & Triceps", max_length=80)
    duration = discord.ui.TextInput(label="Duration (minutes)", placeholder="45", max_length=4)
    workout_date = discord.ui.TextInput(label="Workout date (YYYY-MM-DD)", placeholder="2026-11-15", max_length=10)
    notes = discord.ui.TextInput(label="Notes (optional)", style=discord.TextStyle.paragraph, required=False, max_length=500)

    def __init__(self, workout_type: str, picker_interaction=None):
        super().__init__()
        self.selected_workout_type = workout_type
        self.picker_interaction = picker_interaction
        examples = {
            'Strength': ('Push Day — Chest & Triceps', 'Bench press, shoulder press, triceps'),
            'Cardio': ('Morning 5K Run', 'Distance, pace, intervals'),
            'Sports': ('Basketball — Full Court Games', 'Games played, drills, performance'),
            'Mobility': ('Full Body Stretch & Recovery', 'Stretches, mobility exercises'),
            'Other': ("Today's Workout", 'Describe the activity completed'),
        }
        title_example, notes_example = examples.get(workout_type, examples['Other'])
        self.workout_title.placeholder = title_example
        self.notes.placeholder = notes_example
        self.workout_date.default = dt.datetime.now(TZ).date().isoformat()
        self.proof = discord.ui.FileUpload(required=True,min_values=1,max_values=5)
        self.add_item(discord.ui.Label(text='Workout proof (image)',component=self.proof))

    async def on_submit(self, interaction: discord.Interaction):
        if not await season_gate.require_open(interaction):
            return
        try:
            minutes = int(str(self.duration))
            day = dt.date.fromisoformat(str(self.workout_date))
            today = dt.datetime.now(TZ).date()
            if not 1 <= minutes <= 1440 or day > today or day < today - dt.timedelta(days=84):
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Please enter 1–1440 minutes and a valid date within the past 84 days.", ephemeral=True)
            return
        # Normal submissions are limited to the current Monday-Sunday ET week.
        # Organizers can authorize a specific late date through /workout_exception.
        week_start=workouts.monday(today)
        async with pool.acquire() as con:
            exception=await con.fetchval('SELECT 1 FROM workout_deadline_exceptions WHERE discord_user_id=$1 AND workout_date=$2',interaction.user.id,day)
        if day < week_start and not exception:
            return await interaction.response.send_message('This workout is past the Sunday 11:59 PM Eastern deadline. Ask an organizer for an exception.',ephemeral=True)
        proof_files=self.proof.values or []
        if not proof_files:
            return await interaction.response.send_message('Workout proof is required.',ephemeral=True)
        if any(not (item.content_type or '').startswith('image/') or item.size > 8*1024*1024 for item in proof_files):
            return await interaction.response.send_message('Each proof must be an image smaller than 8 MB.', ephemeral=True)
        if sum(item.size for item in proof_files) > 25*1024*1024:
            return await interaction.response.send_message('Combined proof images must be under 25 MB.', ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        if self.picker_interaction:
            try:
                await self.picker_interaction.delete_original_response()
            except (discord.NotFound, discord.HTTPException):
                pass
        await ensure_player(interaction.user)
        # Re-upload to the journal rather than relying on expiring modal attachment URLs.
        proof_channel=None
        async with pool.acquire() as con:
            journal_id=await con.fetchval('SELECT journal_channel_id FROM players WHERE discord_user_id=$1',interaction.user.id)
        if journal_id:
            proof_channel=bot.get_channel(int(journal_id))
        if proof_channel is None:
            await interaction.followup.send('Your personal journal is not ready. Ask an organizer to approve your plan before logging workouts.',ephemeral=True)
            return
        # Calculate the previous workout XP before inserting the new workout.
        _, _, before_workout_xp = await weekly_progress(interaction.user.id, day)
        try:
            proof_uploads = []
            for index, item in enumerate(proof_files, 1):
                ext = item.filename.rsplit('.', 1)[-1].lower() if '.' in item.filename else 'png'
                proof_uploads.append(await item.to_file(filename=f'workout-proof-{index}.{ext}'))
        except (discord.HTTPException, discord.Forbidden):
            log.exception('Could not download workout proof images')
            await interaction.followup.send('Could not prepare your proof images. Workout not saved; try again.', ephemeral=True)
            return

        workout_id = uuid.uuid4()
        try:
            async with pool.acquire() as con:
                async with con.transaction():
                    await con.execute("""insert into workouts(id,discord_user_id,workout_date,workout_type,duration_minutes,notes,visibility,title,image_discord_url,public_message_id)
                        values($1,$2,$3,$4,$5,$6,'public',$7,$8,NULL)""", workout_id, interaction.user.id,
                        day, self.selected_workout_type, minutes, str(self.notes).strip(),
                        str(self.workout_title).strip(), proof_files[0].url)
                    await con.execute("insert into sync_outbox(workout_id,event_type) values($1,'upsert') on conflict do nothing", workout_id)
        except Exception:
            log.exception('Could not save workout %s', workout_id)
            await interaction.followup.send('Could not save your workout. Please try again.', ephemeral=True)
            return

        # Achievement refresh must follow the database insert. Assemble the final
        # layout before sending, so the journal message is never edited for XP.
        try:
            unlocked = await achievements.refresh(bot, pool, interaction.user.id)
            _, _, after_workout_xp = await weekly_progress(interaction.user.id, day)
            earned = max(0, after_workout_xp - before_workout_xp)
        except Exception:
            log.exception('Could not calculate XP for saved workout %s', workout_id)
            await interaction.followup.send('Workout saved, but XP could not be calculated. Ask an organizer to check your log.', ephemeral=True)
            return

        safe_title = discord.utils.escape_markdown(str(self.workout_title).strip())
        details = (f"## 💪 {safe_title}\n"
                   f"**Activity:** {self.selected_workout_type}  ·  "
                   f"**Duration:** {minutes} minutes  ·  **Date:** {day}")
        if str(self.notes).strip():
            details += "\n**Notes:** " + discord.utils.escape_markdown(str(self.notes).strip())

        xp_lines = []
        if earned:
            xp_lines.append(f"✨ **+{earned} XP** — Workout completed: {safe_title}")
        if unlocked:
            from fitness_quest.achievements import BY_KEY
            for key in unlocked:
                name = discord.utils.escape_markdown(BY_KEY[key][2])
                xp_lines.append(f"✨ **+15 XP** — Achievement unlocked: {name}")

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.TextDisplay(details))
        view.add_item(discord.ui.MediaGallery(*[
            discord.MediaGalleryItem(media='attachment://' + item.filename)
            for item in proof_uploads
        ]))
        if not xp_lines:
            # Always show the XP result, including when the weekly workout XP
            # cap has been reached. Do not claim XP was awarded when it wasn't.
            xp_lines.append("✨ **+0 XP** — Workout logged (no additional XP awarded; weekly cap may have been reached).")
        xp_card = discord.ui.Container(accent_colour=discord.Colour.gold())
        xp_card.add_item(discord.ui.TextDisplay("\n".join(xp_lines)))
        view.add_item(xp_card)

        try:
            proof_message = await proof_channel.send(
                view=view, files=proof_uploads,
                allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, discord.Forbidden):
            log.exception('Workout %s saved but journal upload failed', workout_id)
            await interaction.followup.send('Workout saved, but its journal post failed. Ask an organizer to check it before submitting again.', ephemeral=True)
            return

        # Store the journal message ID for future lookups. Do not edit the message.
        proof_url = next((a.url for a in proof_message.attachments), None)
        if not proof_url:
            proof_url = proof_files[0].url
        try:
            async with pool.acquire() as con:
                await con.execute("""update workouts
                    set public_message_id=$1, image_discord_url=$2
                    where id=$3""", proof_message.id, proof_url, workout_id)
        except Exception:
            log.exception('Journal message %s sent, but workout metadata update failed', proof_message.id)
        # Deferred interaction acknowledged silently; no hub success follow-up.


class WorkoutTypeSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Choose your workout type...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Strength", emoji="🏋️", description="Weights and resistance training"),
                discord.SelectOption(label="Cardio", emoji="🏃", description="Running, cycling, and other cardio"),
                discord.SelectOption(label="Sports", emoji="⚽", description="Team or individual sports"),
                discord.SelectOption(label="Mobility", emoji="🧘", description="Stretching, yoga, and recovery"),
                discord.SelectOption(label="Other", emoji="✨", description="Anything else"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.view.owner_id:
            await interaction.response.send_message("Please use Log Workout to start your own form.", ephemeral=True)
            return
        await interaction.response.send_modal(WorkoutModal(self.values[0], self.view.origin_interaction))


class WorkoutTypeView(discord.ui.View):
    def __init__(self, owner_id: int, origin_interaction=None):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.origin_interaction = origin_interaction
        self.add_item(WorkoutTypeSelect())



# Delete only the exact ephemeral followup message, never the source dashboard.
def dismiss_hub_message(message, seconds=60):
    async def _delete():
        await asyncio.sleep(seconds)
        try:
            await message.delete()
        except (discord.NotFound, discord.HTTPException):
            pass
    asyncio.create_task(_delete())

class MilestoneChapterSelect(discord.ui.Select):
    def __init__(self, owner_id: int):
        self.owner_id = owner_id
        super().__init__(placeholder='Submit milestone proof for chapter…', options=[
            discord.SelectOption(label=f'Chapter {n}', value=str(n)) for n in (1, 2, 3)])

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message('Open your own milestones from the dashboard.', ephemeral=True)
            return
        chapter = int(self.values[0])
        async with pool.acquire() as con:
            row = await con.fetchrow("""SELECT chapter,goal_status,completion_status
                FROM personal_milestones WHERE discord_user_id=$1 AND chapter=$2""",
                interaction.user.id, chapter)
        if not row or row['goal_status'] != 'approved' or row['completion_status'] not in ('not_submitted', 'rejected'):
            await interaction.response.send_message('This milestone is not approved, or its completion is already pending or completed.', ephemeral=True)
            return
        await interaction.response.send_modal(quest_reviews.MilestoneProof(pool, row))


class MilestoneSubmitView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=300)
        self.add_item(MilestoneChapterSelect(owner_id))


class HubView(discord.ui.View):
    @discord.ui.button(label="Begin Your Adventure", style=discord.ButtonStyle.primary, emoji="⚔️", custom_id="fitnessquest:begin_adventure", row=0)
    async def begin_adventure(self, interaction: discord.Interaction, button: discord.ui.Button):
        await quest_progress.start_onboarding(interaction,pool,bot)

    @discord.ui.button(label="Leaderboard", style=discord.ButtonStyle.secondary, emoji="🏆", custom_id="fitnessquest:leaderboard", row=0)
    async def leaderboard_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        rows=await quests.scores(pool)
        view=quests.RankingView(pool,rows)
        message = await interaction.followup.send(embed=view.embed('overall'),view=view,ephemeral=True,wait=True)
        dismiss_hub_message(message)

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Log Workout", style=discord.ButtonStyle.success, emoji="💪", custom_id="fitnessquest:log_workout", row=0)
    async def log_workout(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "**Choose your workout type:**",
            view=WorkoutTypeView(interaction.user.id, interaction),
            ephemeral=True,
        )

    @discord.ui.button(label="My Progress", style=discord.ButtonStyle.secondary, emoji="📊", custom_id="fitnessquest:my_progress", row=0)
    async def progress(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await ensure_player(interaction.user)
        async with pool.acquire() as con:
            chosen_name = await con.fetchval(
                "SELECT challenger_name FROM players WHERE discord_user_id=$1", interaction.user.id)
        if not chosen_name:
            message = await interaction.followup.send("Register first using `/register` to view your graphical profile.", ephemeral=True,wait=True)
            dismiss_hub_message(message)
            return
        await achievements.refresh(bot,pool,interaction.user.id)
        xp = await achievements.total_xp(pool, interaction.user.id)
        stats = await achievements.profile_stats(pool, interaction.user.id)
        goals_list = await workouts.approved_fitness_goals(pool, interaction.user.id)
        count, target, _ = await workouts.get_week(
            pool, interaction.user.id, workouts.monday(workouts.today()))
        avatar = None
        try:
            avatar = await interaction.user.display_avatar.read()
        except discord.HTTPException:
            pass
        img = await asyncio.to_thread(
            workouts.make_profile, chosen_name, xp, count, target, avatar, stats, goals_list)
        message = await interaction.followup.send(
            file=discord.File(img, filename="fitness-quest-profile.png"), ephemeral=True,wait=True)
        dismiss_hub_message(message)

    @discord.ui.button(label="My Goals", style=discord.ButtonStyle.secondary, emoji="🎯", custom_id="fitnessquest:goals", row=1)
    async def goals_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with pool.acquire() as con:
            rows = await con.fetch("""SELECT goal_number, goal_text FROM fitness_goals
                WHERE discord_user_id=$1 AND status='approved'
                AND source_request_id=(SELECT max(source_request_id) FROM fitness_goals
                    WHERE discord_user_id=$1 AND status='approved') ORDER BY goal_number""", interaction.user.id)
        if rows:
            lines = '\n'.join(f"{r['goal_number']}. {discord.utils.escape_markdown(r['goal_text'])}" for r in rows)
            content = '**Your approved fitness goals**\n' + lines
        else:
            content = 'No approved structured fitness goals yet. Use `/set_goal` to submit or update your goals.'
        await interaction.response.send_message(content, ephemeral=True)
        dismiss_hub_message(await interaction.original_response())

    @discord.ui.button(label="Weekly Quests", style=discord.ButtonStyle.secondary, emoji="📜", custom_id="fitnessquest:quests", row=1)
    async def quests_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        week = workouts.monday(workouts.today())
        async with pool.acquire() as con:
            rows = await con.fetch("""SELECT id,kind,title,instructions,xp,proof_type FROM quests
                WHERE week_start=$1 AND published=true AND release_date <= $2 AND due_date >= $2 ORDER BY kind,id""", week, dt.datetime.now(TZ).date())
        embed = discord.Embed(title='📜 Weekly Quest Board', description=f'Week of {week}', color=0x8876c9)
        for q in rows:
            embed.add_field(name=f"{'⚔️' if q['kind']=='main' else '🎨'} #{q['id']} {quests.safe(q['title'])} · {q['xp']} XP",
                            value=quests.safe(q['instructions'])[:900]+'\nProof: '+str(q['proof_type']), inline=False)
        if not rows:
            embed.description += '\nNo published quests yet.'
        view = quests.QuestSelectView(pool, rows) if rows else None
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        dismiss_hub_message(await interaction.original_response())

    @discord.ui.button(label="Weekly Reflection", style=discord.ButtonStyle.secondary, emoji="📖", custom_id="fitnessquest:reflection", row=1)
    async def reflection_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(quests.ReflectionModal(pool))

    @discord.ui.button(label="My Milestones", style=discord.ButtonStyle.secondary, emoji="🏁", custom_id="fitnessquest:milestones", row=1)
    async def milestones_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with pool.acquire() as con:
            rows = await con.fetch("""SELECT chapter,goal,goal_status,completion_status
                FROM personal_milestones WHERE discord_user_id=$1 ORDER BY chapter""", interaction.user.id)
        if not rows:
            await interaction.response.send_message('No milestones yet. Use Begin Your Adventure to set them.', ephemeral=True)
            dismiss_hub_message(await interaction.original_response())
            return
        embed = discord.Embed(title='🏁 Your Milestones', color=0x8876c9)
        for r in rows:
            embed.add_field(name=f'Chapter {r["chapter"]} · Week {r["chapter"]*4}',
                            value=f'{quest_reviews.escape(r["goal"])}\nGoal: {r["goal_status"]} · Completion: {r["completion_status"]} · 30 XP', inline=False)
        await interaction.response.send_message(embed=embed, view=MilestoneSubmitView(interaction.user.id), ephemeral=True)
        dismiss_hub_message(await interaction.original_response())

    @discord.ui.button(label="Achievements", style=discord.ButtonStyle.secondary, emoji="🏆", custom_id="fitnessquest:achievements", row=1)
    async def achievements_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await achievements.refresh(bot, pool, interaction.user.id)
        state = await achievements.snapshot(pool, interaction.user.id)
        unlocked = state['unlocked']
        total = len(achievements.ACHIEVEMENTS)
        embed = discord.Embed(title='🏆 Your Achievements',
            description=f'{len(unlocked)}/{total} unlocked · {len(unlocked)*15}/{total*15} XP', color=discord.Color.gold())
        for key, emoji, title, description in achievements.ACHIEVEMENTS:
            is_unlocked = key in unlocked

            if key in achievements.HIDDEN_ACHIEVEMENTS and not is_unlocked:
                emoji = "❔"
                title = "???"
                description = "Secret achievement. Unlock it to reveal the requirement."

            embed.add_field(
                name=f'{emoji} {title} ' + ('✅' if is_unlocked else '🔒'),
                value=description,
                inline=False
            )
        message = await interaction.followup.send(embed=embed, ephemeral=True, wait=True)
        dismiss_hub_message(message)


HUB_TEXT = "# ⚔️ Fitness Quest\nBegin your adventure, log workouts, view your progress, or check the leaderboard!"

@bot.tree.command(name="log_workout", description="Log a workout without finding the hub button", guild=discord.Object(id=GUILD_ID))
async def log_workout_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**Choose your workout type:**",
        view=WorkoutTypeView(interaction.user.id, interaction), ephemeral=True)

@bot.tree.command(name="setup_hub", description="Create or refresh the permanent workout dashboard", guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
async def setup_hub(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Manage Server permission required.", ephemeral=True)
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Run this in a text channel.", ephemeral=True)
        return
    async with pool.acquire() as con:
        await con.execute("""CREATE TABLE IF NOT EXISTS bot_hub_messages (
            guild_id bigint NOT NULL, channel_id bigint NOT NULL,
            message_id bigint NOT NULL, PRIMARY KEY(guild_id,channel_id))""")
        message_id = await con.fetchval(
            "SELECT message_id FROM bot_hub_messages WHERE guild_id=$1 AND channel_id=$2",
            interaction.guild_id, interaction.channel_id)
    existing = None
    if message_id:
        try:
            existing = await interaction.channel.fetch_message(message_id)
        except discord.NotFound:
            pass
    if existing is None:
        async for candidate in interaction.channel.history(limit=100):
            if candidate.author.id == bot.user.id and candidate.content.startswith("# ⚔️ Fitness Quest"):
                existing = candidate
                break
    if existing:
        await existing.edit(content=HUB_TEXT, view=HubView())
        message = existing
        result = "Existing hub dashboard updated."
    else:
        message = await interaction.channel.send(HUB_TEXT, view=HubView())
        result = "Hub dashboard created."
    async with pool.acquire() as con:
        await con.execute("""INSERT INTO bot_hub_messages(guild_id,channel_id,message_id)
            VALUES($1,$2,$3) ON CONFLICT(guild_id,channel_id)
            DO UPDATE SET message_id=excluded.message_id""",
            interaction.guild_id, interaction.channel_id, message.id)
    await interaction.response.send_message(result, ephemeral=True)


@bot.tree.command(name="workout_image", description="Attach an image to your latest workout", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(image="Workout photo or screenshot")
async def workout_image(interaction: discord.Interaction, image: discord.Attachment):
    if not image.content_type or not image.content_type.startswith("image/") or image.size > 8 * 1024 * 1024:
        await interaction.response.send_message("Please attach an image under 8 MB.", ephemeral=True)
        return
    async with pool.acquire() as con:
        row = await con.fetchrow("""select id,public_message_id from workouts
            where discord_user_id=$1 order by created_at desc limit 1""", interaction.user.id)
        if not row:
            await interaction.response.send_message("Log a workout first, then attach your photo.", ephemeral=True)
            return
        await con.execute("update workouts set image_discord_url=$1 where id=$2", image.url, row['id'])
        await con.execute("""insert into sync_outbox(workout_id,event_type) values($1,'upsert')
            on conflict(workout_id,event_type) do update set processed_at=null""", row['id'])
    if row['public_message_id']:
        await workouts.refresh_journal_post(bot, pool, row['id'], interaction.user.id)
    else:
        try:
            await workouts.publish_journal(bot, pool, row['id'], interaction.user.id)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Photo saved but journal publish failed")
    await interaction.response.send_message(
        "📷 Photo linked to your latest workout and journal post (if available). Discord attachment URLs may expire.", ephemeral=True)


@bot.tree.command(name="workout_exception", description="Admin: allow one late workout date", guild=discord.Object(id=GUILD_ID))
async def workout_exception(interaction:discord.Interaction,member:discord.Member,workout_date:str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message('Manage Server permission required.',ephemeral=True)
    try:
        day=dt.date.fromisoformat(workout_date)
        if day>dt.datetime.now(TZ).date(): raise ValueError
    except ValueError:
        return await interaction.response.send_message('Use a valid past date YYYY-MM-DD.',ephemeral=True)
    async with pool.acquire() as con:
        await con.execute('INSERT INTO workout_deadline_exceptions(discord_user_id,workout_date,approved_by) VALUES($1,$2,$3) ON CONFLICT DO NOTHING',member.id,day,interaction.user.id)
    await interaction.response.send_message(f'Late workout allowed for {member.display_name} on {day}.',ephemeral=True)

@bot.tree.command(name="my_workouts", description="Show details of your five latest workouts", guild=discord.Object(id=GUILD_ID))
async def my_workouts(interaction: discord.Interaction):
    async with pool.acquire() as con:
        rows = await con.fetch("""select title,workout_date,workout_type,duration_minutes,notes,image_discord_url
            from workouts where discord_user_id=$1 order by created_at desc limit 5""", interaction.user.id)
    if not rows:
        await interaction.response.send_message("No workouts yet.", ephemeral=True)
        return
    embeds = []
    for i, r in enumerate(rows, 1):
        title = r['title'] or f"{r['workout_type']} Workout"
        embed = discord.Embed(
            title=f"{i}. {discord.utils.escape_markdown(title)}",
            description=f"📅 {r['workout_date']}  •  🏷️ {r['workout_type']}  •  ⏱️ {r['duration_minutes']} min",
            color=discord.Color.purple())
        if r['notes']:
            embed.add_field(name="Notes", value=discord.utils.escape_markdown(r['notes'][:900]), inline=False)
        if r['image_discord_url']:
            embed.add_field(name="Photo", value=f"[View photo]({r['image_discord_url']})", inline=False)
        embeds.append(embed)
    await interaction.response.send_message(content="💪 **Your Recent Workouts**", embeds=embeds, ephemeral=True)


def sync_sheet_once():
    """Synchronous worker called in a thread. Upserts rows by workout UUID."""
    import gspread
    client = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    spreadsheet = client.open_by_key(SHEET_ID)
    try:
        sheet = spreadsheet.worksheet("Workout Logs")
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="Workout Logs", rows=1000, cols=10)
    return sheet


@tasks.loop(seconds=60)
async def sheet_sync():
    if not SHEET_ID or not os.path.isfile(SERVICE_ACCOUNT_FILE):
        return
    async with pool.acquire() as con:
        pending = await con.fetch("""select o.id as outbox_id,w.* from sync_outbox o join workouts w on w.id=o.workout_id
            where o.processed_at is null order by o.id limit 50""")
    if not pending:
        return
    try:
        sheet = await asyncio.to_thread(sync_sheet_once)
        headers = ["Workout ID", "Discord User ID", "Date", "Type", "Minutes", "Notes", "Visibility", "Image URL", "Created At"]
        values = await asyncio.to_thread(sheet.get_all_values)
        if not values or values[0] != headers:
            await asyncio.to_thread(sheet.update, range_name="A1:I1", values=[headers])
            values = await asyncio.to_thread(sheet.get_all_values)
        existing = {row[0]: i+1 for i,row in enumerate(values[1:], start=1) if row}
        for r in pending:
            row = [str(r["id"]),str(r["discord_user_id"]),str(r["workout_date"]),r["workout_type"],r["duration_minutes"],r["notes"],r["visibility"],r["image_discord_url"] or "",str(r["created_at"])]
            if str(r["id"]) in existing:
                idx = existing[str(r["id"])]
                await asyncio.to_thread(sheet.update, range_name=f"A{idx}:I{idx}", values=[row], value_input_option="RAW")
            else:
                await asyncio.to_thread(sheet.append_row, row, value_input_option="RAW")
            async with pool.acquire() as con:
                await con.execute("update sync_outbox set processed_at=now() where id=$1", r["outbox_id"])
    except Exception:
        log.exception("Google Sheets sync failed; will retry")


@sheet_sync.before_loop
async def before_sync():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    log.info("Connected as %s", bot.user)
    # Startup is silent: restore missing collectible roles without replaying old announcements.
    try:
        await level_roles.reconcile(bot, pool)
    except Exception:
        log.exception('Level-role startup reconciliation failed')


async def main():
    global pool
    log.info("Connecting to Supabase...")
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, ssl="require")
    log.info("Supabase connection pool initialized")
    await level_roles.initialize(pool)
    try:
        async with bot:
            bot.add_view(HubView())
            await workouts.restore_reviews(bot, pool)
            await quest_progress.restore_reviews(bot,pool)
            await bot.login(TOKEN)
            await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
            log.info("Discord slash commands synchronized")
            sheet_sync.start()
            calendar_loop.start()
            await bot.connect()
    finally:
        if calendar_loop.is_running():
            calendar_loop.cancel()
        if sheet_sync.is_running():
            sheet_sync.cancel()
        await pool.close()
        log.info("Supabase connection pool closed")


if __name__ == "__main__":
    asyncio.run(main())
