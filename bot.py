import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import logging.handlers
import queue
import os
import sys
import subprocess
from datetime import datetime, timedelta
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
_log_fmt = logging.Formatter("%(asctime)s AEST [%(levelname)s] %(message)s")
_file_handler   = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
_stream_handler = logging.StreamHandler()
_file_handler.setFormatter(_log_fmt)
_stream_handler.setFormatter(_log_fmt)
_log_queue    = queue.SimpleQueue()
_queue_handler = logging.handlers.QueueHandler(_log_queue)
logging.root.setLevel(logging.INFO)
logging.root.handlers.clear()
logging.root.addHandler(_queue_handler)
_log_listener = logging.handlers.QueueListener(_log_queue, _file_handler, _stream_handler, respect_handler_level=True)
_log_listener.start()
log = logging.getLogger(__name__)

DATA_DIR = os.path.join(BASE_DIR, "data")
IS_DEV   = os.environ.get("BOT_ENV", "").lower() == "dev"



class StockBot(commands.Bot):
    def __init__(self):
        intents         = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
        await self.load_extension("cogs.restock")
        # Skip tree.sync() on restart — commands haven't changed
        if "--restarted" not in sys.argv:
            try:
                async with asyncio.timeout(30):
                    await self.tree.sync()
                log.info("Slash commands synced")
            except asyncio.TimeoutError:
                log.warning("tree.sync() timed out — continuing without sync")
        else:
            log.info("Restart — skipping tree.sync()")

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (ID: {self.user.id}){' [DEV MODE]' if IS_DEV else ''}")
        if not scheduled_restart.is_running():
            scheduled_restart.start()

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
    ts    = datetime.now(ZoneInfo("UTC"))
    total = 4 if is_admin else 2

    p1 = discord.Embed(title="📖 cata.ai — Overview", color=0x5865F2, timestamp=ts)
    p1.add_field(name="📊 Tracker (/rst)", value=(
        "`/rst status` — Tracker state, stores, and per-store channels\n"
        "`/rst subscribe` — Subscribe to alerts with optional filters\n"
        "`/rst unsubscribe` — Remove a subscription by ID\n"
        "`/rst subscriptions` — List your active subscriptions\n"
        "`/rst store [store]` — Store info, channel, and subscribers\n"
        "`/rst user [user]` — A user's subscriptions\n"
        "`/rst catalog [store]` — Browse all products with stock status\n"
        "`/rst search [query] [stores...]` — Search for a product\n"
        "`/rst help` — Detailed /rst command list"
    ), inline=False)
    if is_admin:
        p1.add_field(name="🔐 Admin (/rst-admin)", value=(
            "`/rst-admin start` — Start monitoring\n"
            "`/rst-admin add` — Add a store\n"
            "`/rst-admin channel` — Set per-store alert channel\n"
            "`/rst-admin subscribe` — Subscribe a user or role\n"
            "`/rst-admin help` — Full admin command list"
        ), inline=False)
    p1.add_field(name="⚙️ Bot", value="`/restart` — Restart the bot  •  `/help` — This page", inline=False)
    p1.set_footer(text=f"cata.ai  •  Page 1 of {total}")

    p2 = discord.Embed(title="📖 cata.ai — /rst Commands", color=0x5865F2, timestamp=ts)
    p2.add_field(name="status", value="Tracker state, poll interval, stores, and per-store channel overrides.", inline=False)
    p2.add_field(name="subscribe [store] [names] [sizes]", value=(
        "Subscribe to restock alerts. All filters optional.\n"
        "**names** — comma-separated keywords, item must contain ALL (AND logic)\n"
        "**sizes** — comma-separated sizes, item must match ANY (fuzzy: S / Small / SMALL all match)"
    ), inline=False)
    p2.add_field(name="unsubscribe <id>", value="Remove one of your subscriptions by its ID.", inline=False)
    p2.add_field(name="subscriptions [user]", value="List your active subscriptions with filters and IDs.", inline=False)
    p2.add_field(name="store [store]", value="Store URL, alert channel, subscribed users and roles.", inline=False)
    p2.add_field(name="user [user]", value="A user's subscriptions. Defaults to yourself.", inline=False)
    p2.add_field(name="catalog [store]", value="Paginated product list — 🟢 full / 🟠 partial / 🔴 sold out — sorted newest first.", inline=False)
    p2.add_field(name="search [query] [stores...]", value="Search for a product by name across up to 5 stores.", inline=False)
    p2.set_footer(text=f"cata.ai  •  Page 2 of {total}")

    pages = [p1, p2]

    if is_admin:
        p3 = discord.Embed(title="🔐 cata.ai — /rst-admin Commands", color=0xEB459E, timestamp=ts)
        p3.add_field(name="Tracker Control", value=(
            "`start [channel]` — Start monitoring and set default alert channel\n"
            "`stop` — Stop monitoring for this server\n"
            "`interval [seconds]` — Set poll interval (60–600s)"
        ), inline=False)
        p3.add_field(name="Store Management", value=(
            "`add [name] [url]` — Add a Shopify store (auto-discovers endpoint)\n"
            "`remove [store...]` — Remove up to 5 stores\n"
            "`channel [store] [channel]` — Set a dedicated channel, thread, or forum for a store\n"
            "`export` — Export store list as a shareable code\n"
            "`import [code]` — Import a store list from an export code"
        ), inline=False)
        p3.add_field(name="Subscriptions", value=(
            "`subscribe [target] [store] [names] [sizes]` — Create a filtered subscription for a user or role\n"
            "`unsubscribe <id>` — Remove any subscription by ID"
        ), inline=False)
        p3.add_field(name="Debug", value=(
            "`recent [store] [channel]` — Post most recently updated item\n"
            "`alert [store] [channel]` — Send a fake restock alert for testing"
        ), inline=False)
        p3.add_field(name="Bot", value="`/restart` — Restart the bot process", inline=False)
        p3.set_footer(text="cata.ai  •  Page 3 of 4  •  Admin only")

        pages.append(p3)

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


bot = StockBot()


@bot.tree.command(name="help", description="Show all commands")
async def cmd_help(interaction: discord.Interaction):
    is_admin = interaction.user.guild_permissions.administrator
    pages    = _build_help_pages(is_admin)
    await interaction.response.send_message(embed=pages[0], view=HelpPaginator(pages, 0), ephemeral=True)


RESTART_HOURS_AEST = {0, 8, 16}  # 12 AM, 8 AM, 4 PM AEST

@tasks.loop(minutes=1)
async def scheduled_restart():
    now = datetime.now(AEST)
    if now.hour in RESTART_HOURS_AEST and now.minute == 0:
        log.info(f"Scheduled restart at {now.strftime('%H:%M')} AEST")
        from cogs.restock import save_state, save_products_cache, load_bot_state, save_bot_state
        cog = bot.cogs.get("RestockCog")
        if cog:
            save_state(cog.state)
            save_products_cache(cog.products_cache)
        args = [sys.executable] + [a for a in sys.argv if a != "--restarted"] + ["--restarted"]
        subprocess.Popen(args, cwd=BASE_DIR)
        os._exit(0)


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
        args = [sys.executable] + [a for a in sys.argv if a != "--restarted"] + ["--restarted"]
        subprocess.Popen(args, cwd=BASE_DIR)
        os._exit(0)

    asyncio.create_task(_do_restart())


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set — add it to .env")
    bot.run(token, log_handler=None)
