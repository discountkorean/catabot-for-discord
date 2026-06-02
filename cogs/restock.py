import discord
from discord import app_commands
from discord.ext import commands, tasks
import tomllib
import json
import requests
import asyncio
import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
import os

log = logging.getLogger(__name__)

MAX_SEARCH_RESULTS = 20


# ── Search result model ───────────────────────────────────────────────────────

class SearchResult:
    def __init__(self, store_name: str, store_url: str, product: dict):
        self.store_name  = store_name
        raw_base         = "/".join(store_url.split("/")[:3])
        scheme, _, dom   = raw_base.partition("://")
        self.store_base  = f"{scheme}://{_display_domain(dom)}"
        self.title       = product.get("title", "Unknown")
        self.handle      = product.get("handle", "")
        self.image_url   = (product.get("images") or [{}])[0].get("src")
        self.product_url = f"{self.store_base}/products/{self.handle}"

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
            title=r.title, url=r.product_url, color=0x5865F2,
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
        if r.image_url:
            embed.set_thumbnail(url=r.image_url)
        embed.add_field(name="Store", value=r.store_name, inline=True)
        embed.add_field(name="Price", value=r.price,      inline=True)
        if r.available:
            lines = "\n".join(f"[{v['size']}]({v['cart_url']})" for v in r.available)
            embed.add_field(name=f"✅ In Stock ({len(r.available)})", value=lines or "—", inline=False)
        if r.unavailable:
            sizes = ", ".join(v["size"] for v in r.unavailable) or "—"
            embed.add_field(name=f"❌ Out of Stock ({len(r.unavailable)})", value=sizes, inline=False)
        embed.set_footer(text=f"Result {self.page + 1} of {total}  •  {bot_footer()}")
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


BASE_DIR       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE    = os.path.join(BASE_DIR, "config.toml")
DATA_DIR       = os.path.join(BASE_DIR, "data")
STATE_FILE     = os.path.join(DATA_DIR, "stock_state.json")
BOT_STATE_FILE = os.path.join(DATA_DIR, "bot_state.json")

os.makedirs(DATA_DIR, exist_ok=True)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


# ── Persistence ──────────────────────────────────────────────────────────────

_config_cache: dict | None = None

def load_config() -> dict:
    global _config_cache
    if _config_cache is None:
        with open(CONFIG_FILE, "rb") as f:
            _config_cache = tomllib.load(f)
    return _config_cache


def bot_footer() -> str:
    version = load_config().get("bot", {}).get("version", "1.0.0")
    return f"cata.ai v{version}"


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


def _guild_dir(guild_id: int | str) -> str:
    return os.path.join(DATA_DIR, str(guild_id))


def _guild_file(guild_id: int | str) -> str:
    return os.path.join(_guild_dir(guild_id), "state.json")


def load_guild_state(guild_id: int | str) -> dict:
    path = _guild_file(guild_id)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"alert_channel_id": None, "extra_stores": {}, "notifications": {}}


def save_guild_state(guild_id: int | str, data: dict):
    os.makedirs(_guild_dir(guild_id), exist_ok=True)
    with open(_guild_file(guild_id), "w") as f:
        json.dump(data, f, indent=2)


def load_all_guilds() -> dict:
    """Load all guild state folders from data/{guild_id}/state.json."""
    guilds = {}
    for entry in os.scandir(DATA_DIR):
        if entry.is_dir() and entry.name.isdigit():
            guild_id = entry.name
            guilds[guild_id] = load_guild_state(guild_id)
    return guilds


# ── Shopify helpers ───────────────────────────────────────────────────────────

def _fetch_products_sync(url: str) -> list:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code in (401, 403) or "password" in r.url:
            return []
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            return []
        return data.get("products", [])
    except requests.HTTPError:
        return []
    except Exception as e:
        log.error(f"Failed to fetch {url}: {e}")
        return []


async def fetch_products(url: str) -> list:
    return await asyncio.to_thread(_fetch_products_sync, url)


def _probe_shopify_sync(url: str) -> bool:
    """Return True if the URL is a valid, reachable Shopify products.json endpoint."""
    for attempt in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code in (401, 403) or "password" in r.url:
                return False
            if not r.ok:
                return False
            data = r.json()
            return isinstance(data, dict) and "products" in data
        except requests.exceptions.Timeout:
            if attempt == 0:
                continue
            return False
        except Exception:
            return False
    return False


async def discover_shopify_url(raw: str) -> str | None:
    """
    Given a human-friendly URL, find the correct Shopify products.json endpoint.
    Tries www. → secure. variants. Returns the working URL or None.
    """
    from urllib.parse import urlparse

    if not raw.startswith("http"):
        raw = "https://" + raw

    domain = urlparse(raw).netloc or raw.split("/")[2]

    # Build candidate URLs in priority order
    candidates = [f"https://{domain}/products.json?limit=1000"]

    if domain.startswith("www."):
        bare = domain[4:]
        candidates.append(f"https://secure.{bare}/products.json?limit=1000")
    elif domain.startswith("secure."):
        bare = domain[7:]
        candidates.append(f"https://www.{bare}/products.json?limit=1000")
    else:
        candidates.append(f"https://www.{domain}/products.json?limit=1000")
        candidates.append(f"https://secure.{domain}/products.json?limit=1000")

    for url in candidates:
        if await asyncio.to_thread(_probe_shopify_sync, url):
            return url

    return None


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


# ── Size normalization ────────────────────────────────────────────────────────

_SIZE_ALIASES: dict[str, str] = {
    # XS
    "xs": "xs", "xsmall": "xs", "xsm": "xs", "xsmall": "xs",
    "xsmall": "xs", "x-small": "xs", "xsmall": "xs", "extrasmall": "xs", "extra-small": "xs",
    # S
    "s": "s", "small": "s", "sm": "s",
    # M
    "m": "m", "med": "m", "medium": "m",
    # L
    "l": "l", "large": "l", "lg": "l",
    # XL
    "xl": "xl", "xlarge": "xl", "x-large": "xl", "extralarge": "xl", "extra-large": "xl",
    # 2XL
    "2xl": "2xl", "xxl": "2xl", "xxlarge": "2xl", "2xlarge": "2xl", "doublexl": "2xl", "double-xl": "2xl",
    # 3XL
    "3xl": "3xl", "xxxl": "3xl", "3xlarge": "3xl", "triplexl": "3xl", "triple-xl": "3xl",
    # 4XL+
    "4xl": "4xl", "xxxxl": "4xl",
    "5xl": "5xl",
}


def _normalize_size(token: str) -> str:
    """Normalize a size token to a canonical form for comparison."""
    cleaned = token.lower().strip().replace(" ", "").replace("-", "").replace("_", "")
    return _SIZE_ALIASES.get(cleaned, cleaned)


def _variant_size_tokens(variant_title: str) -> list[str]:
    """Extract and normalize size tokens from a Shopify variant title like 'Black / Small'."""
    parts = []
    for segment in variant_title.replace(",", "/").split("/"):
        parts.append(_normalize_size(segment.strip()))
    return parts


def _sub_matches(sub: dict, store_name: str, variant: dict) -> bool:
    """Return True if a subscription's filters match the given store + variant."""
    if sub["stores"] and store_name not in sub["stores"]:
        return False
    if sub["names"]:
        search_text = (variant["title"] + " " + variant["variant_title"]).lower()
        if not all(kw.lower() in search_text for kw in sub["names"]):
            return False
    if sub["sizes"]:
        vtokens = _variant_size_tokens(variant["variant_title"])
        if not any(s in vtokens for s in sub["sizes"]):
            return False
    return True


def _migrate_notifications(gs: dict) -> bool:
    """
    Convert legacy notifications dict to subscriptions list in-place.
    Returns True if migration occurred.
    """
    if "notifications" not in gs:
        return False
    changed = False
    subs = gs.setdefault("subscriptions", [])
    existing_ids = {(s["type"], s["target_id"]) for s in subs}
    for store_name, notifs in gs["notifications"].items():
        if isinstance(notifs, list):
            notifs = {"users": notifs, "roles": []}
        for uid in notifs.get("users", []):
            if ("user", uid) not in existing_ids:
                subs.append({"id": uuid.uuid4().hex[:8], "type": "user", "target_id": uid,
                             "stores": [store_name], "names": [], "sizes": []})
                existing_ids.add(("user", uid))
                changed = True
        for rid in notifs.get("roles", []):
            if ("role", rid) not in existing_ids:
                subs.append({"id": uuid.uuid4().hex[:8], "type": "role", "target_id": rid,
                             "stores": [store_name], "names": [], "sizes": []})
                existing_ids.add(("role", rid))
                changed = True
    if changed:
        del gs["notifications"]
    return changed


# ── Embeds ────────────────────────────────────────────────────────────────────

def _display_domain(domain: str) -> str:
    if domain.startswith("secure."):
        return "www." + domain[len("secure."):]
    return domain


def _format_sizes(variant_titles: list[str]) -> tuple[str, str]:
    """Returns (field_name, field_value) — hides 'Default Title' for non-variant products."""
    filtered = [t for t in variant_titles if t.lower() != "default title"]
    if not filtered:
        return "Variants", "N/A"
    return "Sizes", ", ".join(filtered)


def _product_url(store_url: str, handle: str) -> str:
    base   = store_url.split("?")[0].rstrip("/products.json").rstrip("/")
    scheme, _, domain_path = base.partition("://")
    parts  = domain_path.split("/", 1)
    domain = _display_domain(parts[0])
    path   = "/" + parts[1] if len(parts) > 1 else ""
    return f"{scheme}://{domain}{path}/products/{handle}"


def make_restock_embed(store_name: str, store_url: str, variants: list) -> discord.Embed:
    first           = variants[0]
    size_name, sizes = _format_sizes([v["variant_title"] for v in variants])
    price           = f"${float(first['price']):.2f}"
    domain          = _display_domain(store_url.split("/")[2])
    embed  = discord.Embed(
        title=f"🔔 Back in Stock: {first['title']}",
        color=0x57F287,
        timestamp=datetime.now(ZoneInfo("UTC")),
    )
    if first.get("image_url"):
        embed.set_thumbnail(url=first["image_url"])
    embed.add_field(name=size_name, value=sizes,          inline=True)
    embed.add_field(name="Price", value=price,             inline=True)
    embed.add_field(name="Store", value=store_name,        inline=True)
    embed.add_field(name="Stock", value="✅ In Stock",     inline=True)
    embed.add_field(name="Link",  value=_product_url(store_url, first["handle"]), inline=False)
    embed.set_footer(text=f"{bot_footer()} • {domain}")
    return embed


def make_new_item_embed(store_name: str, store_url: str, variants: list) -> discord.Embed:
    first     = variants[0]
    price     = f"${float(first['price']):.2f}"
    domain    = _display_domain(store_url.split("/")[2])
    in_stock  = [v["variant_title"] for v in variants if v["available"] and v["variant_title"].lower() != "default title"]
    out_stock = [v["variant_title"] for v in variants if not v["available"] and v["variant_title"].lower() != "default title"]
    has_variants = bool(in_stock or out_stock)
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
    embed.add_field(name="Sizes" if has_variants else "Variants", value=size_lines or "N/A", inline=True)
    embed.add_field(name="Price", value=price,             inline=True)
    embed.add_field(name="Store", value=store_name,        inline=True)
    embed.add_field(name="Link",  value=_product_url(store_url, first["handle"]), inline=False)
    embed.set_footer(text=f"{bot_footer()} • {domain}")
    return embed


def make_removed_embed(store_name: str, store_url: str, variants: list) -> discord.Embed:
    first              = variants[0]
    size_name, sizes   = _format_sizes([v["variant_title"] for v in variants])
    domain             = _display_domain(store_url.split("/")[2])
    embed  = discord.Embed(
        title=f"🗑️ Item Removed: {first['title']}",
        color=0x95a5a6,
        timestamp=datetime.now(ZoneInfo("UTC")),
    )
    if first.get("image_url"):
        embed.set_thumbnail(url=first["image_url"])
    embed.add_field(name=f"Last Known {size_name}", value=sizes, inline=True)
    embed.add_field(name="Store",            value=store_name, inline=True)
    embed.set_footer(text=f"{bot_footer()} • {domain}")
    return embed


def make_sold_out_embed(store_name: str, store_url: str, variants: list) -> discord.Embed:
    first              = variants[0]
    size_name, sizes   = _format_sizes([v["variant_title"] for v in variants])
    price              = f"${float(first['price']):.2f}"
    domain             = _display_domain(store_url.split("/")[2])
    embed  = discord.Embed(
        title=f"🔴 Sold Out: {first['title']}",
        color=0xED4245,
        timestamp=datetime.now(ZoneInfo("UTC")),
    )
    if first.get("image_url"):
        embed.set_thumbnail(url=first["image_url"])
    embed.add_field(name=size_name, value=sizes,           inline=True)
    embed.add_field(name="Price", value=price,              inline=True)
    embed.add_field(name="Store", value=store_name,         inline=True)
    embed.add_field(name="Link",  value=_product_url(store_url, first["handle"]), inline=False)
    embed.set_footer(text=f"{bot_footer()} • {domain}")
    return embed


# ── Cog ───────────────────────────────────────────────────────────────────────

DEFAULT_POLL_INTERVAL = 60

def _default_guild() -> dict:
    return {
        "alert_channel_id": None,
        "stores":           {},
        "subscriptions":    [],
        "poll_interval":    DEFAULT_POLL_INTERVAL,
    }


class RestockCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot        = bot
        self.state      = load_state()
        raw             = load_bot_state()
        self.guilds: dict      = load_all_guilds()
        self._last_polled: dict = {}   # guild_id_str → last poll timestamp

        # Detect legacy single-guild format and migrate in on_ready
        if not self.guilds and ("alert_channel_id" in raw or "guilds" in raw):
            self._legacy_state = raw

    # ── Guild state helpers ───────────────────────────────────────────────────

    def _guild(self, guild_id: int) -> dict:
        key = str(guild_id)
        if key not in self.guilds:
            self.guilds[key] = _default_guild()
        gs = self.guilds[key]
        # Migrate legacy extra_stores → stores
        if "extra_stores" in gs and "stores" not in gs:
            gs["stores"] = gs.pop("extra_stores")
        # Ensure all expected keys exist
        gs.setdefault("stores", {})
        gs.setdefault("subscriptions", [])
        gs.setdefault("poll_interval", DEFAULT_POLL_INTERVAL)
        # Migrate old notifications dict → subscriptions list
        if "notifications" in gs:
            if _migrate_notifications(gs):
                save_guild_state(key, gs)
                log.info(f"Migrated notifications → subscriptions for guild {key}")
        return gs

    def _guild_stores(self, guild_id: int) -> dict:
        return self._guild(guild_id).get("stores", {})

    def _all_stores(self) -> dict:
        stores = {}
        for gs in self.guilds.values():
            stores.update(gs.get("stores", {}))
        return stores

    def _min_interval(self) -> int:
        intervals = [
            gs.get("poll_interval", DEFAULT_POLL_INTERVAL)
            for gs in self.guilds.values()
            if gs.get("alert_channel_id")
        ]
        return min(intervals) if intervals else DEFAULT_POLL_INTERVAL

    def persist(self, guild_id: int | str = None):
        """Save global bot state and optionally one guild, or all guilds."""
        raw = load_bot_state()
        for key in ("alert_channel_id", "extra_stores", "notifications", "guilds", "poll_interval"):
            raw.pop(key, None)
        save_bot_state(raw)

        if guild_id is not None:
            save_guild_state(guild_id, self.guilds[str(guild_id)])
        else:
            for gid, gs in self.guilds.items():
                save_guild_state(gid, gs)

    # ── Poll loop ─────────────────────────────────────────────────────────────

    @tasks.loop(seconds=60)
    async def poll(self):
        # Adjust loop to minimum interval across all active guilds
        min_iv = self._min_interval()
        if self.poll.seconds != min_iv:
            self.poll.change_interval(seconds=min_iv)

        now        = datetime.now(ZoneInfo("UTC")).timestamp()
        all_stores = self._all_stores()

        # Determine which guilds are due for a poll this cycle
        due_guilds = {
            gid: gs for gid, gs in self.guilds.items()
            if gs.get("alert_channel_id") and
               now - self._last_polled.get(gid, 0) >= gs.get("poll_interval", DEFAULT_POLL_INTERVAL)
        }

        if not due_guilds:
            return

        # Collect stores needed by due guilds only
        due_stores = {}
        for gs in due_guilds.values():
            due_stores.update(gs.get("stores", {}))

        for store_name, url in due_stores.items():
            log.info(f"Checking {store_name}...")
            products = await fetch_products(url)
            if not products:
                continue

            current  = build_variant_map(products)
            previous = self.state.get(url)

            # Cold-start: seed silently, no alerts
            if previous is None:
                self.state[url] = current
                log.info(f"Seeded {store_name} ({len(current)} variants)")
                continue

            restocked, new_items, sold_out, removed = {}, {}, {}, {}
            for vid, info in current.items():
                handle = info["handle"]
                if vid not in previous:
                    new_items.setdefault(handle, []).append(info)
                elif not previous[vid].get("available", True) and info["available"]:
                    restocked.setdefault(handle, []).append(info)
                elif previous[vid].get("available", True) and not info["available"]:
                    sold_out.setdefault(handle, []).append(info)

            # Detect fully removed products (variants in previous but not in current)
            for vid, info in previous.items():
                if vid not in current:
                    removed.setdefault(info["handle"], []).append(info)

            self.state[url] = current

            if not restocked and not new_items and not sold_out and not removed:
                continue

            # Route alerts to each due guild that monitors this store
            for guild_id_str, gs in due_guilds.items():
                channel_id = gs.get("alert_channel_id")
                if not channel_id:
                    continue
                channel = self.bot.get_channel(channel_id)
                if not channel:
                    continue

                # Only alert if this store is in this guild's store list
                if store_name not in gs.get("stores", {}):
                    continue

                def _ping_for(variants_list: list) -> str | None:
                    user_ids, role_ids = set(), set()
                    for sub in gs.get("subscriptions", []):
                        for v in variants_list:
                            if _sub_matches(sub, store_name, v):
                                (user_ids if sub["type"] == "user" else role_ids).add(sub["target_id"])
                                break
                    parts = [f"<@{uid}>" for uid in user_ids] + [f"<@&{rid}>" for rid in role_ids]
                    return " ".join(parts) if parts else None

                for variants in restocked.values():
                    await channel.send(content=_ping_for(variants), embed=make_restock_embed(store_name, url, variants))
                    log.info(f"RESTOCK: {variants[0]['title']} @ {store_name} → guild {guild_id_str}")

                for variants in new_items.values():
                    await channel.send(content=_ping_for(variants), embed=make_new_item_embed(store_name, url, variants))
                    log.info(f"NEW ITEM: {variants[0]['title']} @ {store_name} → guild {guild_id_str}")

                for variants in sold_out.values():
                    await channel.send(embed=make_sold_out_embed(store_name, url, variants))
                    log.info(f"SOLD OUT: {variants[0]['title']} @ {store_name} → guild {guild_id_str}")

                for variants in removed.values():
                    await channel.send(embed=make_removed_embed(store_name, url, variants))
                    log.info(f"REMOVED: {variants[0]['title']} @ {store_name} → guild {guild_id_str}")

        save_state(self.state)

        # Stamp last polled time for all due guilds
        for gid in due_guilds:
            self._last_polled[gid] = now

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    # ── Command groups ────────────────────────────────────────────────────────

    restock = app_commands.Group(name="rs",  description="Restock monitor commands")
    tracker = app_commands.Group(name="rst", description="Restock tracker commands")
    admin   = app_commands.Group(
        name="admin",
        description="Admin-only tracker commands",
        parent=tracker,
        default_permissions=discord.Permissions(administrator=True),
    )

    async def _store_autocomplete(self, interaction: discord.Interaction, current: str):
        stores = self._guild_stores(interaction.guild_id)
        return [
            app_commands.Choice(name=n, value=n)
            for n in stores if current.lower() in n.lower()
        ][:25]

    # ── Public commands (/rst) ────────────────────────────────────────────────

    @tracker.command(name="status", description="Show current tracker status")
    async def tracker_status(self, interaction: discord.Interaction):
        await interaction.response.defer()
        gs      = self._guild(interaction.guild_id)
        running = self.poll.is_running()
        stores  = self._guild_stores(interaction.guild_id)
        ch_id   = gs.get("alert_channel_id")
        channel = self.bot.get_channel(ch_id) if ch_id else None

        embed = discord.Embed(
            title="📊 Tracker Status",
            color=0x57F287 if running else 0xED4245,
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
        interval = gs.get("poll_interval", DEFAULT_POLL_INTERVAL)
        embed.add_field(name="State",    value="🟢 Running" if running else "🔴 Stopped",  inline=True)
        embed.add_field(name="Interval", value=f"{interval}s ({interval // 60}m)",         inline=True)
        embed.add_field(name="Channel",  value=channel.mention if channel else "Not set",  inline=True)
        embed.add_field(name="Stores",   value="\n".join(f"• {n}" for n in stores) or "None — use `/rst admin add`", inline=False)
        embed.set_footer(text=bot_footer())
        await interaction.followup.send(embed=embed)

    @tracker.command(name="subscribe", description="Subscribe to restock alerts with optional filters")
    @app_commands.describe(
        store_name="Only notify for this store (leave blank for all stores)",
        names="Comma-separated keywords — item must contain ALL of them (e.g. black,zip-up)",
        sizes="Comma-separated sizes — item must match ANY (e.g. small,xs)",
    )
    @app_commands.autocomplete(store_name=_store_autocomplete)
    async def tracker_subscribe(self, interaction: discord.Interaction,
                                store_name: str = None,
                                names: str = None,
                                sizes: str = None):
        await interaction.response.defer(ephemeral=True)
        gs     = self._guild(interaction.guild_id)
        stores = self._guild_stores(interaction.guild_id)

        if store_name and store_name not in stores:
            await interaction.followup.send(f"❌ **{store_name}** is not a monitored store.", ephemeral=True)
            return

        name_list  = [k.strip().lower() for k in names.split(",") if k.strip()] if names else []
        size_list  = [_normalize_size(s) for s in sizes.split(",") if s.strip()] if sizes else []
        store_list = [store_name] if store_name else []

        duplicate = next((s for s in gs["subscriptions"]
                          if s["type"] == "user"
                          and s["target_id"] == interaction.user.id
                          and sorted(s["stores"]) == sorted(store_list)
                          and sorted(s["names"])  == sorted(name_list)
                          and sorted(s["sizes"])  == sorted(size_list)), None)
        if duplicate:
            await interaction.followup.send(
                f"You already have an identical subscription `[{duplicate['id']}]`. Use `/rst subscriptions` to view yours.",
                ephemeral=True,
            )
            return

        sub = {
            "id":        uuid.uuid4().hex[:8],
            "type":      "user",
            "target_id": interaction.user.id,
            "stores":    store_list,
            "names":     name_list,
            "sizes":     size_list,
        }
        gs["subscriptions"].append(sub)
        self.persist(interaction.guild_id)

        embed = discord.Embed(title="🔔 Subscription Created", color=0x57F287,
                              timestamp=datetime.now(ZoneInfo("UTC")))
        embed.add_field(name="ID",     value=f"`{sub['id']}`",                                inline=True)
        embed.add_field(name="Store",  value=store_name or "All stores",                      inline=True)
        embed.add_field(name="Names",  value=", ".join(name_list) if name_list else "Any",    inline=False)
        embed.add_field(name="Sizes",  value=", ".join(size_list) if size_list else "Any",    inline=False)
        embed.set_footer(text=f"Use /rst unsubscribe {sub['id']} to remove  •  {bot_footer()}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tracker.command(name="unsubscribe", description="Remove one of your subscriptions by ID")
    @app_commands.describe(sub_id="Subscription ID shown in /rst subscriptions")
    async def tracker_unsubscribe(self, interaction: discord.Interaction, sub_id: str):
        await interaction.response.defer(ephemeral=True)
        gs      = self._guild(interaction.guild_id)
        is_admin = interaction.user.guild_permissions.administrator

        before = len(gs["subscriptions"])
        gs["subscriptions"] = [
            s for s in gs["subscriptions"]
            if not (s["id"] == sub_id and (is_admin or (s["type"] == "user" and s["target_id"] == interaction.user.id)))
        ]

        if len(gs["subscriptions"]) == before:
            await interaction.followup.send(f"❌ No subscription with ID `{sub_id}` found (or it's not yours).", ephemeral=True)
            return

        self.persist(interaction.guild_id)
        await interaction.followup.send(f"✅ Removed subscription `{sub_id}`.", ephemeral=True)

    @tracker.command(name="subscriptions", description="List your active subscriptions")
    @app_commands.describe(user="User to inspect (admin only; defaults to you)")
    async def tracker_subscriptions(self, interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer(ephemeral=True)
        gs     = self._guild(interaction.guild_id)
        target = user or interaction.user

        if user and user != interaction.user and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("❌ Only admins can view other users' subscriptions.", ephemeral=True)
            return

        subs = [s for s in gs["subscriptions"] if s["type"] == "user" and s["target_id"] == target.id]

        if not subs:
            await interaction.followup.send(f"No active subscriptions for {target.mention}.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🔔 Subscriptions for {target.display_name}",
            color=0x5865F2,
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
        for s in subs:
            lines = [
                f"**Store:** {', '.join(s['stores']) if s['stores'] else 'All'}",
                f"**Names:** {', '.join(s['names']) if s['names'] else 'Any'}",
                f"**Sizes:** {', '.join(s['sizes']) if s['sizes'] else 'Any'}",
            ]
            embed.add_field(name=f"`{s['id']}`", value="\n".join(lines), inline=False)
        embed.set_footer(text=bot_footer())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tracker.command(name="store", description="Show subscribers and info for a store")
    @app_commands.describe(store_name="Store to inspect")
    @app_commands.autocomplete(store_name=_store_autocomplete)
    async def tracker_store(self, interaction: discord.Interaction, store_name: str):
        await interaction.response.defer()
        gs     = self._guild(interaction.guild_id)
        stores = self._guild_stores(interaction.guild_id)

        if store_name not in stores:
            await interaction.followup.send(f"❌ **{store_name}** is not a monitored store.")
            return

        store_url = stores[store_name]
        domain    = _display_domain(store_url.split("/")[2])
        base_url  = f"https://{domain}"

        relevant = [s for s in gs.get("subscriptions", []) if not s["stores"] or store_name in s["stores"]]
        user_subs = [s for s in relevant if s["type"] == "user"]
        role_subs = [s for s in relevant if s["type"] == "role"]

        def _sub_line(s: dict) -> str:
            filters = []
            if s["names"]: filters.append(f"names: {', '.join(s['names'])}")
            if s["sizes"]: filters.append(f"sizes: {', '.join(s['sizes'])}")
            return " · ".join(filters) if filters else "all items"

        user_lines = []
        for s in user_subs:
            member = interaction.guild.get_member(s["target_id"])
            mention = member.mention if member else f"<@{s['target_id']}>"
            user_lines.append(f"{mention} — {_sub_line(s)} `[{s['id']}]`")

        role_lines = []
        for s in role_subs:
            role = interaction.guild.get_role(s["target_id"])
            mention = role.mention if role else f"<@&{s['target_id']}>"
            role_lines.append(f"{mention} — {_sub_line(s)} `[{s['id']}]`")

        embed = discord.Embed(title=f"🏪 {store_name}", url=base_url, color=0x5865F2, timestamp=datetime.now(ZoneInfo("UTC")))
        embed.add_field(name="URL", value=base_url, inline=False)
        embed.add_field(name=f"👤 Users ({len(user_lines)})", value="\n".join(user_lines) if user_lines else "None", inline=False)
        embed.add_field(name=f"🏷️ Roles ({len(role_lines)})", value="\n".join(role_lines) if role_lines else "None", inline=False)
        embed.set_footer(text=bot_footer())
        await interaction.followup.send(embed=embed)

    @tracker.command(name="user", description="Show a user's subscriptions")
    @app_commands.describe(user="User to inspect (defaults to you)")
    async def tracker_user(self, interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer()
        gs     = self._guild(interaction.guild_id)
        target = user or interaction.user
        stores = self._guild_stores(interaction.guild_id)

        subs = [s for s in gs.get("subscriptions", []) if s["type"] == "user" and s["target_id"] == target.id]

        embed = discord.Embed(title=target.display_name, color=0x5865F2, timestamp=datetime.now(ZoneInfo("UTC")))
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Username", value=str(target), inline=True)

        if subs:
            lines = []
            for s in subs:
                store_label = ", ".join(s["stores"]) if s["stores"] else "All stores"
                filters = []
                if s["names"]: filters.append(f"names: {', '.join(s['names'])}")
                if s["sizes"]: filters.append(f"sizes: {', '.join(s['sizes'])}")
                filter_str = " · ".join(filters) if filters else "all items"
                lines.append(f"**{store_label}** — {filter_str} `[{s['id']}]`")
            embed.add_field(name=f"🔔 Subscriptions ({len(subs)})", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="🔔 Subscriptions", value="None", inline=False)

        embed.set_footer(text=bot_footer())
        await interaction.followup.send(embed=embed)

    def _resolve_channel(self, guild_id: int, override: discord.TextChannel = None):
        if override:
            return override, None
        ch_id = self._guild(guild_id).get("alert_channel_id")
        if ch_id:
            ch = self.bot.get_channel(ch_id)
            if ch:
                return ch, None
        return None, "❌ No alert channel set — run `/rst admin start` first, or pass a `channel` argument."

    # ── Admin commands (/rst admin) ───────────────────────────────────────────

    @admin.command(name="start", description="Start monitoring and set the alert channel")
    @app_commands.describe(channel="Channel to send alerts to (defaults to current channel)")
    async def admin_start(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        await interaction.response.defer()
        gs            = self._guild(interaction.guild_id)
        alert_channel = channel or interaction.channel
        gs["alert_channel_id"] = alert_channel.id

        if "stores" not in gs:
            gs["stores"] = {}

        self.persist(interaction.guild_id)

        if not self.poll.is_running():
            self.poll.start()

        stores     = self._guild_stores(interaction.guild_id)
        store_list = "\n".join(f"• {name}" for name in stores) or "No stores added yet — use `/rst admin add`"
        embed = discord.Embed(
            title="🟢 Tracker Started",
            description=f"Alerts → {alert_channel.mention}\n\n**{len(stores)}** store(s) monitored:\n{store_list}",
            color=0x5865F2,
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
        embed.set_footer(text=bot_footer())
        await interaction.followup.send(embed=embed)

    @admin.command(name="stop", description="Stop monitoring for this server")
    async def admin_stop(self, interaction: discord.Interaction):
        await interaction.response.defer()
        gs = self._guild(interaction.guild_id)
        gs["alert_channel_id"] = None
        self.persist(interaction.guild_id)

        # Stop the loop only if no guild has an active channel
        any_active = any(g.get("alert_channel_id") for g in self.guilds.values())
        if not any_active and self.poll.is_running():
            self.poll.cancel()
            await interaction.followup.send("🔴 Tracker stopped (no active servers remaining).")
        else:
            # Recalculate loop interval now this guild is inactive
            new_min = self._min_interval()
            if self.poll.is_running() and self.poll.seconds != new_min:
                self.poll.change_interval(seconds=new_min)
            await interaction.followup.send("🔴 Alerts disabled for this server.")

    @admin.command(name="interval", description="Set this server's poll interval (min 60s, max 600s)")
    @app_commands.describe(seconds="Interval in seconds (min 60, max 600)")
    async def admin_interval(self, interaction: discord.Interaction, seconds: int):
        await interaction.response.defer()
        if seconds < 60 or seconds > 600:
            await interaction.followup.send(f"❌ Interval must be between **60s** and **600s**. Got `{seconds}s`.")
            return
        gs = self._guild(interaction.guild_id)
        gs["poll_interval"] = seconds
        self.persist(interaction.guild_id)

        # Update the loop to the new minimum interval if needed
        new_min = self._min_interval()
        if self.poll.is_running() and self.poll.seconds != new_min:
            self.poll.change_interval(seconds=new_min)

        await interaction.followup.send(f"✅ Poll interval for this server updated to **{seconds}s** ({seconds // 60}m {seconds % 60}s).")

    @admin.command(name="add", description="Add a Shopify store to monitor")
    @app_commands.describe(store_name="Display name for the store", url="Store URL (e.g. https://www.houndarchives.com)")
    async def admin_add(self, interaction: discord.Interaction, store_name: str, url: str):
        await interaction.response.defer()
        gs = self._guild(interaction.guild_id)

        await interaction.followup.send(f"🔍 Checking **{store_name}**...", ephemeral=True)

        discovered = await discover_shopify_url(url)
        if not discovered:
            await interaction.followup.send(
                f"❌ Could not find a Shopify storefront at **{url}**.\n"
                f"The store may be password-protected, not on Shopify, or currently down.",
                ephemeral=True,
            )
            return

        gs["stores"][store_name] = discovered
        self.persist(interaction.guild_id)
        domain = _display_domain(discovered.split("/")[2])
        await interaction.followup.send(f"✅ Added **{store_name}**\n🔗 `https://{domain}`")

    @admin.command(name="export", description="Export this server's store list as a shareable code")
    async def admin_export(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        gs     = self._guild(interaction.guild_id)
        stores = gs.get("stores", {})

        if not stores:
            await interaction.followup.send("❌ No stores to export.", ephemeral=True)
            return

        import base64, io
        code       = base64.urlsafe_b64encode(json.dumps(stores).encode()).decode()
        store_list = "\n".join(f"• {name}" for name in stores)
        embed = discord.Embed(
            title="📤 Store Export",
            description=f"Use `/rst admin import` with the attached code file on another server.\n\n**{len(stores)} store(s):**\n{store_list}",
            color=0x5865F2,
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
        embed.set_footer(text=bot_footer())
        file = discord.File(io.BytesIO(code.encode()), filename="stores-export.txt")
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)

    @admin.command(name="import", description="Import a store list from an export code")
    @app_commands.describe(code="Export code from /rst admin export")
    async def admin_import(self, interaction: discord.Interaction, code: str):
        await interaction.response.defer(ephemeral=True)

        import base64
        try:
            padded = code.strip()
            padded += '=' * (-len(padded) % 4)
            stores = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
            if not isinstance(stores, dict):
                raise ValueError
        except Exception:
            await interaction.followup.send("❌ Invalid code.", ephemeral=True)
            return

        gs = self._guild(interaction.guild_id)
        existing      = gs.get("stores", {})
        existing_urls = set(existing.values())

        added, skipped = {}, []
        for name, url in stores.items():
            if name in existing or url in existing_urls:
                skipped.append(name)
            else:
                added[name] = url
                existing_urls.add(url)

        gs["stores"].update(added)
        self.persist(interaction.guild_id)

        lines = []
        if added:   lines.append(f"✅ Imported {len(added)} store(s): " + ", ".join(f"**{n}**" for n in added))
        if skipped: lines.append(f"⏭️ Skipped {len(skipped)} (already present by name or URL): " + ", ".join(f"**{n}**" for n in skipped))
        await interaction.followup.send("\n".join(lines) or "No stores imported.", ephemeral=True)

    @admin.command(name="remove", description="Remove one or more stores from monitoring")
    @app_commands.describe(
        store1="Store to remove", store2="Additional store", store3="Additional store",
        store4="Additional store", store5="Additional store",
    )
    @app_commands.autocomplete(store1=_store_autocomplete, store2=_store_autocomplete,
                               store3=_store_autocomplete, store4=_store_autocomplete,
                               store5=_store_autocomplete)
    async def admin_remove(self, interaction: discord.Interaction,
                           store1: str, store2: str = None, store3: str = None,
                           store4: str = None, store5: str = None):
        await interaction.response.defer()
        gs            = self._guild(interaction.guild_id)
        names                  = [s for s in [store1, store2, store3, store4, store5] if s]
        removed, not_found     = [], []

        for name in names:
            if name in gs["stores"]:
                del gs["stores"][name]
                removed.append(name)
            else:
                not_found.append(name)

        if removed:
            self.persist(interaction.guild_id)

        lines = []
        if removed:   lines.append("✅ Removed: "   + ", ".join(f"**{n}**" for n in removed))
        if not_found: lines.append("❌ Not found: " + ", ".join(f"**{n}**" for n in not_found))
        await interaction.followup.send("\n".join(lines) or "No changes made.")

    @admin.command(name="subscribe", description="Create a filtered subscription for a user or role")
    @app_commands.describe(
        target="User or role to subscribe",
        store_name="Only notify for this store (leave blank for all)",
        names="Comma-separated keywords — item must contain ALL of them",
        sizes="Comma-separated sizes — item must match ANY",
    )
    @app_commands.autocomplete(store_name=_store_autocomplete)
    async def admin_subscribe(self, interaction: discord.Interaction,
                              target: discord.Member | discord.Role,
                              store_name: str = None, names: str = None, sizes: str = None):
        await interaction.response.defer(ephemeral=True)
        gs = self._guild(interaction.guild_id)

        stores = self._guild_stores(interaction.guild_id)
        if store_name and store_name not in stores:
            await interaction.followup.send(f"❌ **{store_name}** is not a monitored store.", ephemeral=True)
            return

        name_list  = [k.strip().lower() for k in names.split(",") if k.strip()] if names else []
        size_list  = [_normalize_size(s) for s in sizes.split(",") if s.strip()] if sizes else []
        store_list = [store_name] if store_name else []
        target_type = "role" if isinstance(target, discord.Role) else "user"

        targets = [(target_type, target)]

        created, skipped = [], []
        for target_type, target in targets:
            duplicate = next((s for s in gs["subscriptions"]
                              if s["type"] == target_type
                              and s["target_id"] == target.id
                              and sorted(s["stores"]) == sorted(store_list)
                              and sorted(s["names"])  == sorted(name_list)
                              and sorted(s["sizes"])  == sorted(size_list)), None)
            if duplicate:
                skipped.append((target, duplicate["id"]))
                continue
            sub = {
                "id":        uuid.uuid4().hex[:8],
                "type":      target_type,
                "target_id": target.id,
                "stores":    store_list,
                "names":     name_list,
                "sizes":     size_list,
            }
            gs["subscriptions"].append(sub)
            created.append((target, sub["id"]))

        if created:
            self.persist(interaction.guild_id)

        embed = discord.Embed(title="🔔 Subscription Results", color=0x57F287,
                              timestamp=datetime.now(ZoneInfo("UTC")))
        embed.add_field(name="Store", value=store_name or "All stores",                   inline=True)
        embed.add_field(name="Names", value=", ".join(name_list) if name_list else "Any", inline=True)
        embed.add_field(name="Sizes", value=", ".join(size_list) if size_list else "Any", inline=True)
        if created:
            embed.add_field(name="✅ Created", value="\n".join(f"{t.mention} `[{id}]`" for t, id in created), inline=False)
        if skipped:
            embed.add_field(name="⏭️ Already exists", value="\n".join(f"{t.mention} `[{id}]`" for t, id in skipped), inline=False)
        embed.set_footer(text=bot_footer())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin.command(name="unsubscribe", description="Remove any subscription by ID")
    @app_commands.describe(sub_id="Subscription ID to remove")
    async def admin_unsubscribe(self, interaction: discord.Interaction, sub_id: str):
        await interaction.response.defer(ephemeral=True)
        gs     = self._guild(interaction.guild_id)
        before = len(gs["subscriptions"])
        gs["subscriptions"] = [s for s in gs["subscriptions"] if s["id"] != sub_id]
        if len(gs["subscriptions"]) == before:
            await interaction.followup.send(f"❌ No subscription with ID `{sub_id}` found.", ephemeral=True)
            return
        self.persist(interaction.guild_id)
        await interaction.followup.send(f"✅ Removed subscription `{sub_id}`.", ephemeral=True)

    @admin.command(name="recent", description="Post the most recently updated item from a store")
    @app_commands.describe(store_name="Store to check", channel="Channel to post in (defaults to tracker channel)")
    @app_commands.autocomplete(store_name=_store_autocomplete)
    async def admin_recent(self, interaction: discord.Interaction,
                           store_name: str, channel: discord.TextChannel = None):
        await interaction.response.defer(ephemeral=True)
        stores = self._guild_stores(interaction.guild_id)

        if store_name not in stores:
            await interaction.followup.send(f"❌ **{store_name}** is not a monitored store.", ephemeral=True)
            return

        dest, err = self._resolve_channel(interaction.guild_id, channel)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        products = await fetch_products(stores[store_name])
        if not products:
            await interaction.followup.send(f"❌ Could not fetch products from **{store_name}**.", ephemeral=True)
            return

        latest      = max(products, key=lambda p: p.get("updated_at", ""))
        images      = latest.get("images", [])
        image_url   = images[0]["src"] if images else None
        variants    = latest.get("variants", [])
        available   = [v for v in variants if v.get("available")]
        unavailable = [v for v in variants if not v.get("available")]
        price       = f"${float(variants[0]['price']):.2f}" if variants else "N/A"
        store_url   = stores[store_name]
        base        = store_url.split("?")[0].rstrip("/products.json").rstrip("/")
        product_url = f"{base}/products/{latest.get('handle', '')}"
        updated_raw = latest.get("updated_at", "")

        embed = discord.Embed(
            title=f"🕐 Most Recent: {latest.get('title', 'Unknown')}",
            url=product_url, color=0x5865F2, timestamp=datetime.now(ZoneInfo("UTC")),
        )
        if image_url:
            embed.set_thumbnail(url=image_url)
        embed.add_field(name="Store", value=store_name, inline=True)
        embed.add_field(name="Price", value=price,      inline=True)
        if available:
            embed.add_field(name=f"✅ In Stock ({len(available)})",      value=", ".join(v.get("title","") for v in available) or "—",   inline=False)
        if unavailable:
            embed.add_field(name=f"❌ Out of Stock ({len(unavailable)})", value=", ".join(v.get("title","") for v in unavailable) or "—", inline=False)
        if updated_raw:
            embed.add_field(name="Last Updated", value=f"<t:{int(datetime.fromisoformat(updated_raw.replace('Z','+00:00')).timestamp())}:R>", inline=False)
        embed.set_footer(text=f"{bot_footer()} • {_display_domain(store_url.split('/')[2])}")

        await dest.send(embed=embed)
        await interaction.followup.send(f"✅ Posted most recent item from **{store_name}** to {dest.mention}.", ephemeral=True)

    @admin.command(name="alert", description="Send a fake restock alert to test ping notifications")
    @app_commands.describe(store_name="Store to simulate", channel="Channel to post in (defaults to tracker channel)")
    @app_commands.autocomplete(store_name=_store_autocomplete)
    async def admin_alert(self, interaction: discord.Interaction,
                          store_name: str, channel: discord.TextChannel = None):
        await interaction.response.defer(ephemeral=True)
        gs     = self._guild(interaction.guild_id)
        stores = self._guild_stores(interaction.guild_id)

        if store_name not in stores:
            await interaction.followup.send(f"❌ **{store_name}** is not a monitored store.", ephemeral=True)
            return

        dest, err = self._resolve_channel(interaction.guild_id, channel)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        store_url     = stores[store_name]
        fake_variants = [{"title": "Debug Product", "variant_title": "M", "price": "99.99",
                          "handle": "debug-product", "image_url": None, "available": True}]

        user_ids, role_ids = set(), set()
        for sub in gs.get("subscriptions", []):
            for v in fake_variants:
                if _sub_matches(sub, store_name, v):
                    (user_ids if sub["type"] == "user" else role_ids).add(sub["target_id"])
                    break
        pings = [f"<@{uid}>" for uid in user_ids] + [f"<@&{rid}>" for rid in role_ids]
        ping  = " ".join(pings) if pings else None

        embed       = make_restock_embed(store_name, store_url, fake_variants)
        embed.title = f"🧪 [DEBUG] {embed.title}"
        embed.color = 0xEB459E

        await dest.send(content=ping, embed=embed)
        await interaction.followup.send(
            f"✅ Fake alert sent to {dest.mention} for **{store_name}**"
            + (f" — pinged {len(pings)} subscriber(s)." if pings else " — no subscribers to ping."),
            ephemeral=True,
        )

    # ── Search ────────────────────────────────────────────────────────────────

    @tracker.command(name="search", description="Search for a product across one or more monitored stores")
    @app_commands.describe(query="Product name or keyword", store1="Store to search",
                           store2="Additional store", store3="Additional store",
                           store4="Additional store", store5="Additional store")
    @app_commands.autocomplete(store1=_store_autocomplete, store2=_store_autocomplete,
                               store3=_store_autocomplete, store4=_store_autocomplete,
                               store5=_store_autocomplete)
    async def restock_search(self, interaction: discord.Interaction, query: str, store1: str,
                             store2: str = None, store3: str = None,
                             store4: str = None, store5: str = None):
        await interaction.response.defer()
        all_stores = self._guild_stores(interaction.guild_id)
        chosen     = [s for s in [store1, store2, store3, store4, store5] if s]
        invalid    = [s for s in chosen if s not in all_stores]
        if invalid:
            await interaction.followup.send(f"❌ Unknown stores: {', '.join(f'**{s}**' for s in invalid)}", ephemeral=True)
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
            await interaction.followup.send(f"No products found matching **{query}** in {store_label}.", ephemeral=True)
            return

        results   = results[:MAX_SEARCH_RESULTS]
        paginator = SearchPaginator(results)
        await interaction.followup.send(embed=paginator.build_embed(), view=paginator)

    # ── Startup ───────────────────────────────────────────────────────────────

    def _purge_user(self, uid: int, guild_id: str):
        """Remove all references to a user from a guild's data."""
        gs = self.guilds.get(guild_id)
        if not gs:
            return
        before = len(gs.get("subscriptions", []))
        gs["subscriptions"] = [s for s in gs.get("subscriptions", [])
                                if not (s["type"] == "user" and s["target_id"] == uid)]
        if len(gs["subscriptions"]) != before:
            self.persist(guild_id)
            log.info(f"Purged all subscriptions for user {uid} from guild {guild_id}")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Purge all user data from the guild they left."""
        self._purge_user(member.id, str(member.guild.id))

    @commands.Cog.listener()
    async def on_ready(self):
        # Purge stale name-keyed entries from stock_state (legacy format pre URL-keying)
        stale_keys = [k for k in self.state if not k.startswith("http")]
        if stale_keys:
            for k in stale_keys:
                del self.state[k]
            save_state(self.state)
            log.info(f"Purged {len(stale_keys)} stale stock state entries")

        # Migrate legacy bot_state formats
        if hasattr(self, "_legacy_state") and self.bot.guilds:
            legacy   = self._legacy_state
            guild_id = str(self.bot.guilds[0].id)

            # Format A: flat single-guild (alert_channel_id at root)
            if legacy.get("alert_channel_id") and not self.guilds:
                extra = legacy.get("extra_stores", {})
                self.guilds[guild_id] = {
                    "alert_channel_id": legacy["alert_channel_id"],
                    "stores":           extra,
                    "notifications":    legacy.get("notifications", {}),
                    "poll_interval":    legacy.get("poll_interval", DEFAULT_POLL_INTERVAL),
                }
                self.persist(guild_id)
                log.info(f"Migrated legacy (flat) bot state to guild {guild_id}")

            # Format B: guilds nested dict in bot_state.json
            elif "guilds" in legacy and not self.guilds:
                for gid, gs in legacy["guilds"].items():
                    extra = gs.get("extra_stores", gs.get("stores", {}))
                    self.guilds[gid] = {
                        "alert_channel_id": gs.get("alert_channel_id"),
                        "stores":           extra,
                        "notifications":    gs.get("notifications", {}),
                    }
                    self.persist(gid)
                log.info(f"Migrated nested guilds dict to per-folder format ({len(legacy['guilds'])} guilds)")

            del self._legacy_state

        # Resume poll if any guild has an active channel
        any_active = any(g.get("alert_channel_id") for g in self.guilds.values())
        if any_active and not self.poll.is_running():
            self.poll.start()

        # Edit restart confirmation message if present
        raw                = load_bot_state()
        restart_channel_id = raw.pop("restart_channel_id", None)
        restart_message_id = raw.pop("restart_message_id", None)
        restart_time       = raw.pop("restart_time", 0)
        if restart_channel_id or restart_message_id:
            save_bot_state(raw)

        elapsed = datetime.now(ZoneInfo("UTC")).timestamp() - restart_time
        if restart_channel_id and restart_message_id and elapsed < 30:
            try:
                channel = self.bot.get_channel(restart_channel_id)
                msg     = await channel.fetch_message(restart_message_id)
                await msg.edit(content="✅ Restarted successfully.")
            except Exception as e:
                log.warning(f"Could not edit restart message: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(RestockCog(bot))
