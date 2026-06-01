import discord
from discord import app_commands
from discord.ext import commands, tasks
import tomllib
import json
import requests
import asyncio
import os
import sys
import subprocess
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

MAX_SEARCH_RESULTS = 20


# ── Search result model ───────────────────────────────────────────────────────

class SearchResult:
    def __init__(self, store_name: str, store_url: str, product: dict):
        self.store_name  = store_name
        self.store_base  = "/".join(store_url.split("/")[:3])
        self.title       = product.get("title", "Unknown")
        self.handle      = product.get("handle", "")
        self.image_url   = (product.get("images") or [{}])[0].get("src")
        self.product_url = f"{self.store_base}/products/{self.handle}"

        # Group variants by size, keep available ones separate
        self.available:   list[dict] = []
        self.unavailable: list[dict] = []
        for v in product.get("variants", []):
            entry = {
                "size":       v.get("title", ""),
                "price":      v.get("price", "0.00"),
                "variant_id": v["id"],
                "cart_url":   f"{self.store_base}/cart/{v['id']}:1",
            }
            (self.available if v.get("available") else self.unavailable).append(entry)

    @property
    def price(self) -> str:
        src = self.available or self.unavailable
        return f"${float(src[0]['price']):.2f}" if src else "N/A"


# ── Search paginator ──────────────────────────────────────────────────────────

class SearchPaginator(discord.ui.View):
    def __init__(self, results: list[SearchResult]):
        super().__init__(timeout=120)
        self.results = results
        self.page    = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page == len(self.results) - 1

    def build_embed(self) -> discord.Embed:
        r     = self.results[self.page]
        total = len(self.results)

        embed = discord.Embed(
            title=r.title,
            url=r.product_url,
            color=0x5865F2,
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
        if r.image_url:
            embed.set_thumbnail(url=r.image_url)

        embed.add_field(name="Store", value=r.store_name, inline=True)
        embed.add_field(name="Price", value=r.price,      inline=True)

        if r.available:
            lines = "\n".join(
                f"[{v['size']}]({v['cart_url']})" for v in r.available
            )
            embed.add_field(name=f"✅ In Stock ({len(r.available)})", value=lines, inline=False)

        if r.unavailable:
            sizes = ", ".join(v["size"] for v in r.unavailable)
            embed.add_field(name=f"❌ Out of Stock ({len(r.unavailable)})", value=sizes, inline=False)

        embed.set_footer(text=f"Result {self.page + 1} of {total}  •  Shopify Stock Monitor")
        return embed

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE    = os.path.join(BASE_DIR, "config.toml")
STATE_FILE     = os.path.join(BASE_DIR, "data", "stock_state.json")
BOT_STATE_FILE = os.path.join(BASE_DIR, "data", "bot_state.json")

os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


# ── Persistence ──────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_bot_state() -> dict:
    if os.path.exists(BOT_STATE_FILE):
        with open(BOT_STATE_FILE) as f:
            return json.load(f)
    return {}


def save_bot_state(data: dict):
    with open(BOT_STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Shopify helpers ───────────────────────────────────────────────────────────

def _fetch_products_sync(url: str) -> list:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json().get("products", [])
    except Exception as e:
        log.error(f"Failed to fetch {url}: {e}")
        return []


async def fetch_products(url: str) -> list:
    return await asyncio.to_thread(_fetch_products_sync, url)


def build_variant_map(products: list) -> dict:
    variants = {}
    for product in products:
        handle    = product.get("handle", "")
        title     = product.get("title", "Unknown")
        images    = product.get("images", [])
        image_url = images[0]["src"] if images else None
        for v in product.get("variants", []):
            variants[str(v["id"])] = {
                "available":     v.get("available", False),
                "title":         title,
                "variant_title": v.get("title", ""),
                "price":         v.get("price", "0.00"),
                "handle":        handle,
                "image_url":     image_url,
            }
    return variants


# ── Embeds ────────────────────────────────────────────────────────────────────

def _product_url(store_url: str, handle: str) -> str:
    base = store_url.split("?")[0].rstrip("/products.json").rstrip("/")
    return f"{base}/products/{handle}"


def make_restock_embed(store_name: str, store_url: str, variants: list) -> discord.Embed:
    first  = variants[0]
    sizes  = ", ".join(v["variant_title"] for v in variants)
    price  = f"${float(first['price']):.2f}"
    domain = store_url.split("/")[2]

    embed = discord.Embed(
        title=f"🔔 Back in Stock: {first['title']}",
        color=0x57F287,
        timestamp=datetime.now(ZoneInfo("UTC")),
    )
    if first.get("image_url"):
        embed.set_thumbnail(url=first["image_url"])
    embed.add_field(name="Sizes",  value=sizes,       inline=True)
    embed.add_field(name="Price",  value=price,        inline=True)
    embed.add_field(name="Store",  value=store_name,   inline=True)
    embed.add_field(name="Stock",  value="✅ In Stock", inline=True)
    embed.add_field(name="Link",   value=_product_url(store_url, first["handle"]), inline=False)
    embed.set_footer(text=f"Shopify Stock Monitor • {domain}")
    return embed


def make_new_item_embed(store_name: str, store_url: str, variants: list) -> discord.Embed:
    first    = variants[0]
    price    = f"${float(first['price']):.2f}"
    domain   = store_url.split("/")[2]
    in_stock  = [v["variant_title"] for v in variants if v["available"]]
    out_stock = [v["variant_title"] for v in variants if not v["available"]]
    size_lines = ""
    if in_stock:
        size_lines += "✅ " + ", ".join(in_stock)
    if out_stock:
        size_lines += ("\n" if size_lines else "") + "❌ " + ", ".join(out_stock)

    embed = discord.Embed(
        title=f"🆕 New Item: {first['title']}",
        color=0xFEE75C,
        timestamp=datetime.now(ZoneInfo("UTC")),
    )
    if first.get("image_url"):
        embed.set_thumbnail(url=first["image_url"])
    embed.add_field(name="Sizes",  value=size_lines,  inline=True)
    embed.add_field(name="Price",  value=price,        inline=True)
    embed.add_field(name="Store",  value=store_name,   inline=True)
    embed.add_field(name="Link",   value=_product_url(store_url, first["handle"]), inline=False)
    embed.set_footer(text=f"Shopify Stock Monitor • {domain}")
    return embed


# ── Cog ───────────────────────────────────────────────────────────────────────

class RestockCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot           = bot
        self.state         = load_state()
        bot_state          = load_bot_state()
        self.channel_id    = bot_state.get("alert_channel_id")
        self.extra_stores: dict      = bot_state.get("extra_stores", {})
        self.notifications: dict     = bot_state.get("notifications", {})

    def get_all_stores(self) -> dict:
        return {**load_config()["stores"], **self.extra_stores}

    def persist(self):
        save_bot_state({
            "alert_channel_id": self.channel_id,
            "extra_stores":     self.extra_stores,
            "notifications":    self.notifications,
        })

    # ── Poll loop ─────────────────────────────────────────────────────────────

    @tasks.loop(seconds=300)
    async def poll(self):
        channel = self.bot.get_channel(self.channel_id)
        if not channel:
            log.warning("Alert channel not found.")
            return

        config = load_config()
        interval = config["monitor"]["poll_interval"]
        if self.poll.seconds != interval:
            self.poll.change_interval(seconds=interval)

        stores = self.get_all_stores()
        for store_name, url in stores.items():
            log.info(f"Checking {store_name}...")
            products = await fetch_products(url)
            if not products:
                continue

            current  = build_variant_map(products)
            previous = self.state.get(store_name, {})
            restocked, new_items = {}, {}

            for vid, info in current.items():
                handle = info["handle"]
                if vid not in previous:
                    new_items.setdefault(handle, []).append(info)
                elif not previous[vid].get("available", True) and info["available"]:
                    restocked.setdefault(handle, []).append(info)

            subscribers = self.notifications.get(store_name, [])
            ping = " ".join(f"<@{uid}>" for uid in subscribers) if subscribers else None

            for variants in restocked.values():
                await channel.send(content=ping, embed=make_restock_embed(store_name, url, variants))
                log.info(f"RESTOCK: {variants[0]['title']} @ {store_name}")

            for variants in new_items.values():
                await channel.send(embed=make_new_item_embed(store_name, url, variants))
                log.info(f"NEW ITEM: {variants[0]['title']} @ {store_name}")

            self.state[store_name] = current

        save_state(self.state)

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    # ── Command groups ────────────────────────────────────────────────────────

    restock = app_commands.Group(name="restock", description="Restock monitor commands")
    tracker = app_commands.Group(name="tracker", description="Manage the restock tracker", parent=restock)

    @tracker.command(name="start", description="Start monitoring and set the alert channel")
    @app_commands.describe(channel="Channel to send alerts to (defaults to current channel)")
    async def tracker_start(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        await interaction.response.defer()
        alert_channel   = channel or interaction.channel
        self.channel_id = alert_channel.id
        self.persist()

        if not self.poll.is_running():
            self.poll.start()

        stores     = self.get_all_stores()
        store_list = "\n".join(f"• {name}" for name in stores)
        embed = discord.Embed(
            title="🟢 Tracker Started",
            description=f"Alerts → {alert_channel.mention}\n\nMonitoring **{len(stores)}** stores:\n{store_list}",
            color=0x5865F2,
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
        embed.set_footer(text="Shopify Stock Monitor")
        await interaction.followup.send(embed=embed)

    @tracker.command(name="stop", description="Stop monitoring")
    async def tracker_stop(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if self.poll.is_running():
            self.poll.cancel()
            await interaction.followup.send("🔴 Tracker stopped.")
        else:
            await interaction.followup.send("Tracker is not running.")

    @tracker.command(name="status", description="Show current tracker status")
    async def tracker_status(self, interaction: discord.Interaction):
        await interaction.response.defer()
        running  = self.poll.is_running()
        stores   = self.get_all_stores()
        config   = load_config()
        interval = config["monitor"]["poll_interval"]

        embed = discord.Embed(
            title="📊 Tracker Status",
            color=0x57F287 if running else 0xED4245,
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
        embed.add_field(name="State",    value="🟢 Running" if running else "🔴 Stopped", inline=True)
        embed.add_field(name="Interval", value=f"{interval}s ({interval // 60}m)",        inline=True)
        embed.add_field(name="Stores",   value="\n".join(f"• {n}" for n in stores),       inline=False)
        embed.set_footer(text="Shopify Stock Monitor")
        await interaction.followup.send(embed=embed)

    @tracker.command(name="add", description="Add a store to monitor")
    @app_commands.describe(store_name="Display name for the store", url="Shopify products.json URL")
    async def tracker_add(self, interaction: discord.Interaction, store_name: str, url: str):
        await interaction.response.defer()
        url = url.split("?")[0].rstrip("/") + "?limit=1000"
        if not url.endswith("products.json?limit=1000"):
            url = url.rstrip("/") + "/products.json?limit=1000"
        self.extra_stores[store_name] = url
        self.persist()
        await interaction.followup.send(f"✅ Added **{store_name}**\n`{url}`")

    async def _store_autocomplete(self, interaction: discord.Interaction, current: str):
        stores = self.get_all_stores()
        return [
            app_commands.Choice(name=n, value=n)
            for n in stores if current.lower() in n.lower()
        ][:25]

    @tracker.command(name="notify", description="Toggle restock ping notifications for a store")
    @app_commands.describe(store_name="Store to toggle notifications for")
    @app_commands.autocomplete(store_name=_store_autocomplete)
    async def tracker_notify(self, interaction: discord.Interaction, store_name: str):
        await interaction.response.defer(ephemeral=True)

        stores = self.get_all_stores()
        if store_name not in stores:
            await interaction.followup.send(f"❌ **{store_name}** is not a monitored store.", ephemeral=True)
            return

        uid  = interaction.user.id
        subs = self.notifications.setdefault(store_name, [])
        if uid in subs:
            subs.remove(uid)
            self.persist()
            await interaction.followup.send(
                f"🔕 You'll no longer be pinged for restocks at **{store_name}**.", ephemeral=True
            )
        else:
            subs.append(uid)
            self.persist()
            await interaction.followup.send(
                f"🔔 You'll be pinged whenever **{store_name}** restocks.", ephemeral=True
            )

    @tracker.command(name="remove", description="Remove one or more stores from monitoring")
    @app_commands.describe(
        store1="Store to remove",
        store2="Additional store to remove",
        store3="Additional store to remove",
        store4="Additional store to remove",
        store5="Additional store to remove",
    )
    @app_commands.autocomplete(store1=_store_autocomplete, store2=_store_autocomplete,
                               store3=_store_autocomplete, store4=_store_autocomplete,
                               store5=_store_autocomplete)
    async def tracker_remove(self, interaction: discord.Interaction,
                             store1: str,
                             store2: str = None, store3: str = None,
                             store4: str = None, store5: str = None):
        await interaction.response.defer()
        names = [s for s in [store1, store2, store3, store4, store5] if s]
        config_stores = load_config()["stores"]
        removed, in_config, not_found = [], [], []

        for name in names:
            if name in self.extra_stores:
                del self.extra_stores[name]
                removed.append(name)
            elif name in config_stores:
                in_config.append(name)
            else:
                not_found.append(name)

        if removed:
            self.persist()

        lines = []
        if removed:
            lines.append("✅ Removed: " + ", ".join(f"**{n}**" for n in removed))
        if in_config:
            lines.append("⚠️ In `config.toml` (remove manually): " + ", ".join(f"**{n}**" for n in in_config))
        if not_found:
            lines.append("❌ Not found: " + ", ".join(f"**{n}**" for n in not_found))
        await interaction.followup.send("\n".join(lines))

    # ── Search ────────────────────────────────────────────────────────────────

    @restock.command(name="search", description="Search for a product across one or more monitored stores")
    @app_commands.describe(
        query="Product name or keyword to search for",
        store1="Store to search in",
        store2="Additional store to search",
        store3="Additional store to search",
        store4="Additional store to search",
        store5="Additional store to search",
    )
    @app_commands.autocomplete(store1=_store_autocomplete, store2=_store_autocomplete,
                               store3=_store_autocomplete, store4=_store_autocomplete,
                               store5=_store_autocomplete)
    async def restock_search(self, interaction: discord.Interaction,
                             query: str, store1: str,
                             store2: str = None, store3: str = None,
                             store4: str = None, store5: str = None):
        await interaction.response.defer()

        all_stores  = self.get_all_stores()
        chosen      = [s for s in [store1, store2, store3, store4, store5] if s]
        invalid     = [s for s in chosen if s not in all_stores]
        if invalid:
            await interaction.followup.send(
                f"❌ Unknown stores: {', '.join(f'**{s}**' for s in invalid)}", ephemeral=True
            )
            return

        results: list[SearchResult] = []
        q = query.lower()

        async def search_store(name: str):
            products = await fetch_products(all_stores[name])
            for product in products:
                if q in product.get("title", "").lower():
                    results.append(SearchResult(name, all_stores[name], product))

        await asyncio.gather(*(search_store(n) for n in chosen))

        store_label = ", ".join(f"**{n}**" for n in chosen)
        if not results:
            await interaction.followup.send(
                f"No products found matching **{query}** in {store_label}.", ephemeral=True
            )
            return

        results = results[:MAX_SEARCH_RESULTS]
        paginator = SearchPaginator(results)
        await interaction.followup.send(embed=paginator.build_embed(), view=paginator)

    # ── Restart confirmation on boot ──────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self):
        bot_state          = load_bot_state()
        restart_channel_id = bot_state.pop("restart_channel_id", None)
        restart_message_id = bot_state.pop("restart_message_id", None)
        restart_time       = bot_state.pop("restart_time", 0)
        if restart_channel_id or restart_message_id:
            save_bot_state(bot_state)

        elapsed = datetime.now(ZoneInfo("UTC")).timestamp() - restart_time
        if restart_channel_id and restart_message_id and elapsed < 30:
            try:
                channel = self.bot.get_channel(restart_channel_id)
                msg = await channel.fetch_message(restart_message_id)
                await msg.edit(content="✅ Restarted successfully.")
            except Exception as e:
                log.warning(f"Could not edit restart message: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(RestockCog(bot))
