import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import logging.handlers
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
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

DATA_DIR = os.path.join(BASE_DIR, "data")
IS_DEV   = os.environ.get("BOT_ENV", "").lower() == "dev"

# Suppress console windows when spawning git subprocesses on Windows
_GIT_FLAGS = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


def _git(args: list, **kwargs):
    return subprocess.run(args, creationflags=_GIT_FLAGS, capture_output=True, text=True, **kwargs)


def _git_pull_data():
    """Pull latest data from private repo. Runs synchronously at startup."""
    if not os.path.isdir(os.path.join(DATA_DIR, ".git")):
        log.warning("data/ is not a git repo — skipping pull")
        return
    result = _git(["git", "pull", "--ff-only"], cwd=DATA_DIR)
    if result.returncode == 0:
        log.info(f"Data pull: {result.stdout.strip() or 'already up to date'}")
    else:
        log.warning(f"Data pull failed: {result.stderr.strip()}")


def _git_push_data():
    """Commit and push any changed data files. Runs in a thread."""
    if not os.path.isdir(os.path.join(DATA_DIR, ".git")):
        return
    _git(["git", "add", "."], cwd=DATA_DIR)
    result = _git(
        ["git", "commit", "-m", f"auto-sync {datetime.now(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S')} UTC"],
        cwd=DATA_DIR,
    )
    if "nothing to commit" in result.stdout:
        return
    push = _git(["git", "push"], cwd=DATA_DIR)
    if push.returncode == 0:
        log.info("Data synced to remote.")
    else:
        log.warning(f"Data push failed: {push.stderr.strip()}")


class StockBot(commands.Bot):
    def __init__(self):
        intents         = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
        if IS_DEV:
            log.info("DEV mode — skipping data pull")
        else:
            await asyncio.to_thread(_git_pull_data)
        self.tree.add_command(help_group)
        await self.load_extension("cogs.restock")
        await self.load_extension("cogs.steam")
        await self.tree.sync()
        log.info("Slash commands synced")

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (ID: {self.user.id}){' [DEV MODE]' if IS_DEV else ''}")
        if not IS_DEV and not self.sync_data_task.is_running():
            self.sync_data_task.start()

    @tasks.loop(minutes=5)
    async def sync_data_task(self):
        await asyncio.to_thread(_git_push_data)

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        msg = "❌ An unexpected error occurred. Please try again."
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ You don't have permission to use this command."
        else:
            log.error(f"Unhandled app command error in /{interaction.command.qualified_name if interaction.command else '?'}: {error}", exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


def _build_help_pages(is_admin: bool) -> list[discord.Embed]:
    ts = datetime.now(ZoneInfo("UTC"))

    p1 = discord.Embed(title="📖 cata.ai — Overview", color=0x5865F2, timestamp=ts)
    p1.add_field(name="📊 Tracker (/rst)", value=(
        "`/rst status` — Tracker state and store list\n"
        "`/rst notify [store]` — Toggle restock pings for yourself\n"
        "`/rst store [store]` — Store info and subscribers\n"
        "`/rst user [user]` — Stores a user is subscribed to\n"
        "`/rst search [query] [stores...]` — Search for a product"
    ), inline=False)
    p1.add_field(name="🎮 Steam (/steam)", value=(
        "`/steam search [query]` — Search for a game on Steam"
    ), inline=False)
    p1.add_field(name="🔍 Help", value=(
        "`/help general` — This overview\n"
        "`/help rst` — Detailed /rst command list"
        + ("\n`/help admin` — Admin commands\n`/help rst-admin` — /rst admin commands" if is_admin else "")
    ), inline=False)
    p1.set_footer(text=f"cata.ai  •  Page 1 of {4 if is_admin else 2}")

    p2 = discord.Embed(title="📖 cata.ai — /rst Commands", color=0x5865F2, timestamp=ts)
    p2.add_field(name="status", value="Show tracker state, poll interval, and monitored stores.", inline=False)
    p2.add_field(name="notify [store]", value="Toggle restock ping notifications for yourself on a store.", inline=False)
    p2.add_field(name="store [store]", value="Show store URL, subscribed users and roles.", inline=False)
    p2.add_field(name="user [user]", value="Show all stores a user is subscribed to. Defaults to yourself.", inline=False)
    p2.add_field(name="search [query] [stores...]", value="Search for a product by name across up to 5 stores.", inline=False)
    p2.set_footer(text=f"cata.ai  •  Page 2 of {4 if is_admin else 2}")

    pages = [p1, p2]

    if is_admin:
        p3 = discord.Embed(title="🔐 cata.ai — Admin Commands", color=0xEB459E, timestamp=ts)
        p3.add_field(name="⚙️ Bot", value="`/restart` — Restart the bot process", inline=False)
        p3.add_field(name="🔗 See also", value="Page 4 → `/rst admin` commands", inline=False)
        p3.set_footer(text="cata.ai  •  Page 3 of 4  •  Admin only")

        p4 = discord.Embed(title="🔐 cata.ai — /rst admin Commands", color=0xEB459E, timestamp=ts)
        p4.add_field(name="Tracker Control", value=(
            "`start [channel]` — Start monitoring\n"
            "`stop` — Stop monitoring\n"
            "`interval [seconds]` — Set poll interval (60–600s)"
        ), inline=False)
        p4.add_field(name="Store Management", value=(
            "`add [name] [url]` — Add a store\n"
            "`remove [store...]` — Remove up to 5 stores"
        ), inline=False)
        p4.add_field(name="Notifications", value=(
            "`notify [store] [user/role]` — Toggle pings for any user or role"
        ), inline=False)
        p4.add_field(name="Debug", value=(
            "`recent [store] [channel]` — Post most recently updated item\n"
            "`alert [store] [channel]` — Send a fake restock alert for testing"
        ), inline=False)
        p4.set_footer(text="cata.ai  •  Page 4 of 4  •  Admin only")

        pages += [p3, p4]

    return pages


class HelpPaginator(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], start: int = 0):
        super().__init__(timeout=120)
        self.pages = pages
        self.page  = start
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page == len(self.pages) - 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)


help_group = app_commands.Group(name="help", description="Command reference")

bot = StockBot()


@help_group.command(name="general", description="Show all public commands")
async def help_general(interaction: discord.Interaction):
    is_admin = interaction.user.guild_permissions.administrator
    pages    = _build_help_pages(is_admin)
    await interaction.response.send_message(embed=pages[0], view=HelpPaginator(pages, 0), ephemeral=True)


@help_group.command(name="rst", description="Show all /rst commands")
async def help_rst(interaction: discord.Interaction):
    is_admin = interaction.user.guild_permissions.administrator
    pages    = _build_help_pages(is_admin)
    await interaction.response.send_message(embed=pages[1], view=HelpPaginator(pages, 1), ephemeral=True)


@help_group.command(name="admin", description="Show all admin commands")
@app_commands.checks.has_permissions(administrator=True)
async def help_admin(interaction: discord.Interaction):
    pages = _build_help_pages(True)
    await interaction.response.send_message(embed=pages[2], view=HelpPaginator(pages, 2), ephemeral=True)


@help_group.command(name="rst-admin", description="Show all /rst admin commands")
@app_commands.checks.has_permissions(administrator=True)
async def help_rst_admin(interaction: discord.Interaction):
    pages = _build_help_pages(True)
    await interaction.response.send_message(embed=pages[3], view=HelpPaginator(pages, 3), ephemeral=True)


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
        subprocess.Popen([sys.executable] + sys.argv, cwd=BASE_DIR, creationflags=_GIT_FLAGS)
        os._exit(0)

    asyncio.create_task(_do_restart())


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set — add it to .env")
    bot.run(token, log_handler=None)
