import asyncio
import discord
from discord import app_commands
from discord.ext import commands
import tomllib
import logging
import os
import sys
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

AEST = ZoneInfo("Australia/Sydney")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "logs", "monitor.log")
PID_FILE = os.path.join(BASE_DIR, "data", "bot.pid")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)

logging.Formatter.converter = lambda *args: datetime.now(AEST).timetuple()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s AEST [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


def load_config() -> dict:
    with open(os.path.join(BASE_DIR, "config.toml"), "rb") as f:
        return tomllib.load(f)


class StockBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
        await self.load_extension("cogs.restock")
        await self.tree.sync()
        log.info("Slash commands synced")

    async def on_ready(self):
        config = load_config()
        activity_text = config["discord"].get("activity", "Shopify stores")
        activity_type = config["discord"].get("activity_type", "watching").lower()
        activity_map = {
            "watching":  discord.ActivityType.watching,
            "playing":   discord.ActivityType.playing,
            "listening": discord.ActivityType.listening,
            "competing": discord.ActivityType.competing,
        }
        await self.change_presence(
            activity=discord.Activity(
                type=activity_map.get(activity_type, discord.ActivityType.watching),
                name=activity_text,
            )
        )
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")


bot = StockBot()


@bot.tree.command(name="restart", description="Restart the bot process")
async def cmd_restart(interaction: discord.Interaction):
    from cogs.restock import load_bot_state, save_bot_state
    await interaction.response.defer()
    msg = await interaction.followup.send("🔄 Restarting...")

    state = load_bot_state()
    state["restart_channel_id"] = interaction.channel_id
    state["restart_message_id"] = msg.id
    state["restart_time"]       = datetime.now(ZoneInfo("UTC")).timestamp()
    save_bot_state(state)

    async def _do_restart():
        await asyncio.sleep(1)
        subprocess.Popen([sys.executable] + sys.argv)
        await bot.close()

    asyncio.create_task(_do_restart())


if __name__ == "__main__":
    config = load_config()
    bot.run(config["discord"]["token"], log_handler=None)
