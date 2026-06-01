import asyncio
import discord
from discord import app_commands
from discord.ext import commands
import logging
import os
import sys
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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



class StockBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
        self.tree.add_command(help_group)
        await self.load_extension("cogs.restock")
        await self.tree.sync()
        log.info("Slash commands synced")

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")


help_group = app_commands.Group(name="help", description="Command reference")

bot = StockBot()

@help_group.command(name="general", description="Show all public commands")
async def help_general(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 cata.ai — Commands", color=0x5865F2, timestamp=datetime.now(ZoneInfo("UTC")))
    embed.add_field(name="📊 Tracker", value=(
        "`/rst status` — Show tracker state and monitored stores\n"
        "`/rst notify [store]` — Toggle restock pings for yourself\n"
        "`/rst store [store]` — Show store info and subscribers\n"
        "`/rst user [user]` — Show a user's subscribed stores\n"
        "`/rst search [query] [stores...]` — Search for a product"
    ), inline=False)
    embed.add_field(name="🔍 General", value="`/help general` — Show this message", inline=False)
    embed.set_footer(text="cata.ai • Admin commands visible via /help admin")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@help_group.command(name="rst", description="Show all /rst commands")
async def help_rst(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 cata.ai — /rst Commands", color=0x5865F2, timestamp=datetime.now(ZoneInfo("UTC")))
    embed.add_field(name="📊 Tracker", value=(
        "`/rst status` — Tracker state, interval, and store list\n"
        "`/rst notify [store]` — Toggle restock pings for yourself\n"
        "`/rst store [store]` — Store info and current subscribers\n"
        "`/rst user [user]` — Stores a user is subscribed to\n"
        "`/rst search [query] [stores...]` — Search for a product across stores"
    ), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@help_group.command(name="admin", description="Show all admin commands")
@app_commands.checks.has_permissions(administrator=True)
async def help_admin(interaction: discord.Interaction):
    embed = discord.Embed(title="🔐 cata.ai — Admin Commands", color=0xEB459E, timestamp=datetime.now(ZoneInfo("UTC")))
    embed.add_field(name="⚙️ Bot", value=(
        "`/restart` — Restart the bot process"
    ), inline=False)
    embed.add_field(name="📋 For /rst admin commands", value="Use `/help rst-admin`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@help_group.command(name="rst-admin", description="Show all /rst admin commands")
@app_commands.checks.has_permissions(administrator=True)
async def help_rst_admin(interaction: discord.Interaction):
    embed = discord.Embed(title="🔐 cata.ai — /rst admin Commands", color=0xEB459E, timestamp=datetime.now(ZoneInfo("UTC")))
    embed.add_field(name="⚙️ Tracker Control", value=(
        "`/rst admin start [channel]` — Start monitoring\n"
        "`/rst admin stop` — Stop monitoring\n"
        "`/rst admin interval [seconds]` — Set poll interval (60–600s)"
    ), inline=False)
    embed.add_field(name="🏪 Store Management", value=(
        "`/rst admin add [name] [url]` — Add a store\n"
        "`/rst admin remove [store...]` — Remove stores"
    ), inline=False)
    embed.add_field(name="🔔 Notifications", value=(
        "`/rst admin notify [store] [user/role]` — Toggle pings for a user or role"
    ), inline=False)
    embed.add_field(name="🧪 Debug", value=(
        "`/rst admin recent [store] [channel]` — Post most recent item\n"
        "`/rst admin alert [store] [channel]` — Send fake restock alert"
    ), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="restart", description="Restart the bot process")
@app_commands.checks.has_permissions(administrator=True)
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
        subprocess.Popen([sys.executable] + sys.argv, cwd=BASE_DIR)
        os._exit(0)

    asyncio.create_task(_do_restart())


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set — add it to .env")
    bot.run(token, log_handler=None)
