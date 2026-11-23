"""Official season submission gate. Registration and plan approvals remain open."""
import datetime as dt
import os
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

def is_open():
    start = dt.date.fromisoformat(os.environ["FITNESS_QUEST_SEASON_START"])
    return dt.datetime.now(ET) >= dt.datetime.combine(start, dt.time.min, ET)

async def require_open(interaction):
    if is_open():
        return True
    start = dt.date.fromisoformat(os.environ["FITNESS_QUEST_SEASON_START"])
    message = f"Workout and XP submissions open {start:%B %d, %Y} at 12:00 AM Eastern Time. Registration and plan setup are available now."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
    return False
