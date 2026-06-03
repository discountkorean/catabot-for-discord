import discord
from discord import app_commands
from discord.ext import commands, tasks
import tomllib
import json
import requests
import asyncio
import logging
import re
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
    def __init__(self, results: list[SearchResult], cog=None, guild_id: int = None):
        super().__init__(timeout=120)
        self.results  = results
        self.cog      = cog
        self.guild_id = guild_id
        self.page     = 0
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

    @discord.ui.button(label="👀 Watch", style=discord.ButtonStyle.primary)
    async def watch_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog or not self.guild_id:
            await interaction.response.send_message("Watch unavailable here.", ephemeral=True)
            return
        r         = self.results[self.page]
        stores    = self.cog._guild_stores(self.guild_id)
        store_url = stores.get(r.store_name, "")
        if not store_url:
            await interaction.response.send_message("Could not find store for this product.", ephemeral=True)
            return
        base      = _base_url(store_url)
        await interaction.response.defer(ephemeral=True)
        try:
            def _fetch_product():
                with _HTTP.get(f"{base}/products/{r.handle}.js", timeout=10) as resp:
                    return _normalize_product_js(resp.json())
            product = await asyncio.to_thread(_fetch_product)
        except Exception:
            await interaction.followup.send("Could not fetch product details. Please try again.", ephemeral=True)
            return
        picker = WatchSizePicker(self.cog, self.guild_id, r.store_name, store_url, product)
        await interaction.followup.send(embed=picker.build_embed(), view=picker, ephemeral=True)


class WatchSizePicker(discord.ui.View):
    """Size-picker UI for watching a product. Shows all variants as toggle buttons."""

    def __init__(self, cog, guild_id: int, store_name: str, store_url: str, product: dict):
        super().__init__(timeout=120)
        self.cog        = cog
        self.guild_id   = guild_id
        self.store_name = store_name
        self.store_url  = store_url

        self.product_title = product.get("title", "Unknown")
        self.handle        = product.get("handle", "")
        base               = _base_url(store_url)
        self.product_url   = f"{base}/products/{self.handle}"
        self.image_url     = (product.get("images") or [{}])[0].get("src")

        self.variants: list[dict] = product.get("variants", [])
        self.selected: set[str]   = set()

        # Add one button per variant (up to 20 to leave room for Confirm/Cancel row)
        for v in self.variants[:20]:
            vid       = str(v["id"])
            avail     = v.get("available", False)
            label     = f"{v.get('title', vid)} {'✅' if avail else '🔴'}"
            btn       = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                custom_id=f"watch_size_{vid}",
            )
            btn.callback = self._make_toggle(vid)
            self.add_item(btn)

        self.confirm_btn = discord.ui.Button(
            label="Confirm", style=discord.ButtonStyle.success, disabled=True, row=4
        )
        self.confirm_btn.callback = self._confirm
        self.add_item(self.confirm_btn)

        cancel_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger, row=4)
        cancel_btn.callback = self._cancel
        self.add_item(cancel_btn)

    def _make_toggle(self, vid: str):
        async def toggle(interaction: discord.Interaction):
            if vid in self.selected:
                self.selected.discard(vid)
            else:
                self.selected.add(vid)
            # Update button styles
            for item in self.children:
                if isinstance(item, discord.ui.Button) and item.custom_id == f"watch_size_{vid}":
                    item.style = discord.ButtonStyle.primary if vid in self.selected else discord.ButtonStyle.secondary
            self.confirm_btn.disabled = len(self.selected) == 0
            await interaction.response.edit_message(view=self)
        return toggle

    async def _confirm(self, interaction: discord.Interaction):
        selected_variants = [v for v in self.variants if str(v["id"]) in self.selected]
        variant_ids    = [str(v["id"]) for v in selected_variants]
        variant_titles = [v.get("title", str(v["id"])) for v in selected_variants]

        # Check for duplicate watch
        gs = self.cog._guild(self.guild_id)
        existing = next((s for s in gs.get("subscriptions", [])
                         if s.get("type") == "watch"
                         and s.get("handle") == self.handle
                         and s.get("target_id") == interaction.user.id
                         and sorted(s.get("variant_ids", [])) == sorted(variant_ids)), None)
        if existing:
            await interaction.response.edit_message(
                content=f"You already have an identical watch `[{existing['id']}]`.",
                view=None, embed=None
            )
            return

        sub = {
            "type":           "watch",
            "id":             str(uuid.uuid4())[:8],
            "target_id":      interaction.user.id,
            "store":          self.store_name,
            "handle":         self.handle,
            "variant_ids":    variant_ids,
            "variant_titles": variant_titles,
        }
        gs.setdefault("subscriptions", []).append(sub)
        self.cog.persist(self.guild_id)

        # Seed state so already-available variants don't re-alert
        state_key = self.store_url
        if state_key in self.cog.state:
            for v in selected_variants:
                vid = str(v["id"])
                if vid not in self.cog.state[state_key]:
                    self.cog.state[state_key][vid] = {
                        "available":     v.get("available", False),
                        "title":         self.product_title,
                        "variant_title": v.get("title", ""),
                        "price":         str(v.get("price", "0.00")),
                        "handle":        self.handle,
                        "image_url":     self.image_url,
                    }
            save_state(self.cog.state)

        sizes_str = ", ".join(variant_titles)
        await interaction.response.edit_message(
            content=f"👀 Watching **{self.product_title}** ({sizes_str}) at **{self.store_name}**. You'll get a DM when it restocks.",
            embed=None, view=None
        )

        # DM for already-in-stock variants
        in_stock = [v for v in selected_variants if v.get("available")]
        if in_stock:
            try:
                sizes_in_stock = ", ".join(v.get("title", "") for v in in_stock)
                await interaction.user.send(
                    f"👀 Heads up — **{sizes_in_stock}** of **{self.product_title}** is already in stock at **{self.store_name}**:\n{self.product_url}"
                )
            except discord.Forbidden:
                pass

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Watch cancelled.", embed=None, view=None)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=self.product_title, url=self.product_url, color=0x5865F2,
            description="Select the variants you want to watch, then click **Confirm**.",
        )
        if self.image_url:
            embed.set_thumbnail(url=self.image_url)
        embed.add_field(name="Store", value=self.store_name, inline=True)
        embed.set_footer(text=bot_footer())
        return embed


class WatchProductSelect(discord.ui.View):
    """Product pick-list shown after /rst watch search results."""

    def __init__(self, cog, guild_id: int, store_name: str, store_url: str, products: list[dict]):
        super().__init__(timeout=120)
        self.cog        = cog
        self.guild_id   = guild_id
        self.store_name = store_name
        self.store_url  = store_url
        self.products   = {p["handle"]: p for p in products}

        options = [
            discord.SelectOption(label=p.get("title", p["handle"])[:100], value=p["handle"])
            for p in products[:10]
        ]
        select = discord.ui.Select(placeholder="Choose a product…", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        handle  = interaction.data["values"][0]
        product = self.products[handle]
        picker  = WatchSizePicker(self.cog, self.guild_id, self.store_name, self.store_url, product)
        await interaction.response.edit_message(embed=picker.build_embed(), view=picker)


CATALOG_PAGE_SIZE = 15


class CatalogPaginator(discord.ui.View):
    def __init__(self, store_name: str, store_url: str, pages: list[list[dict]]):
        super().__init__(timeout=180)
        self.store_name = store_name
        self.store_url  = store_url
        self.pages      = pages
        self.page       = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page == len(self.pages) - 1

    def build_embed(self) -> discord.Embed:
        items  = self.pages[self.page]
        domain = _display_domain(self.store_url.split("/")[2])
        embed  = discord.Embed(
            title=f"🛍️ {self.store_name} Catalog",
            url=f"https://{domain}",
            color=0x5865F2,
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
        lines = []
        for item in items:
            variants  = item["variants"]
            n_avail   = sum(1 for v in variants if v.get("available"))
            n_total   = len(variants)
            if n_avail == n_total:
                dot = "🟢"
            elif n_avail == 0:
                dot = "🔴"
            else:
                dot = "🟠"
            price = f"${min(float(v['price']) for v in variants):.2f}" if variants else "N/A"
            lines.append(f"{dot} **{item['title']}** — {price}")
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"🟢 In Stock  🟠 Partial  🔴 Sold Out  •  Page {self.page + 1} of {len(self.pages)}  •  {bot_footer()} • {domain}")
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


ALERT_TYPES = [
    ("restock",  "🔁 Restock"),
    ("new_item", "🆕 New Item"),
    ("sold_out", "🔴 Sold Out"),
    ("removed",  "🗑️ Removed"),
]

def _default_store_alerts() -> dict:
    return {
        "restock":  True,
        "new_item": True,
        "sold_out": False,
        "removed":  False,
    }


class AlertToggleView(discord.ui.View):
    """4 toggle buttons shown on /rst store — enable/disable alert types per store."""

    def __init__(self, cog, guild_id: int, store_name: str):
        super().__init__(timeout=300)
        self.cog        = cog
        self.guild_id   = guild_id
        self.store_name = store_name
        self._rebuild()

    def _alerts(self) -> dict:
        gs           = self.cog._guild(self.guild_id)
        store_alerts = gs.setdefault("store_alerts", {})
        if self.store_name not in store_alerts:
            store_alerts[self.store_name] = _default_store_alerts()
            self.cog.persist(self.guild_id)
        return store_alerts[self.store_name]

    def _rebuild(self):
        self.clear_items()
        alerts = self._alerts()
        for key, label in ALERT_TYPES:
            enabled = alerts.get(key, True)
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary,
                custom_id=f"alert_toggle_{key}",
            )
            btn.callback = self._make_callback(key)
            self.add_item(btn)

    def _make_callback(self, key: str):
        async def callback(interaction: discord.Interaction):
            gs           = self.cog._guild(self.guild_id)
            store_alerts = gs.setdefault("store_alerts", {})
            if self.store_name not in store_alerts:
                store_alerts[self.store_name] = _default_store_alerts()
            alerts = store_alerts[self.store_name]
            alerts[key] = not alerts.get(key, True)
            self.cog.persist(self.guild_id)
            self._rebuild()
            await interaction.response.edit_message(view=self)
        return callback


class ATCView(discord.ui.View):
    """Link-button row for Add-to-Cart on restock/new-item alerts."""

    def __init__(self, store_url: str, variants: list[dict]):
        super().__init__(timeout=None)
        from urllib.parse import urlparse
        p       = urlparse(store_url)
        base    = f"{p.scheme}://{p.netloc}"
        domain  = _display_domain(p.netloc)
        available = [v for v in variants if v.get("available") and v.get("variant_id")]
        for v in available[:20]:
            label = v.get("variant_title", "")
            if not label or label.lower() == "default title":
                label = "Buy Now"
            self.add_item(discord.ui.Button(
                label=label[:80],
                url=f"https://{domain}/cart/{v['variant_id']}:1",
                style=discord.ButtonStyle.link,
            ))

    @property
    def has_buttons(self) -> bool:
        return len(self.children) > 0


BASE_DIR       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE    = os.path.join(BASE_DIR, "config.toml")
DATA_DIR       = os.path.join(BASE_DIR, "data")
STATE_FILE         = os.path.join(DATA_DIR, "stock_state.json")
BOT_STATE_FILE     = os.path.join(DATA_DIR, "bot_state.json")
PRODUCTS_CACHE_FILE = os.path.join(DATA_DIR, "products_cache.json")

os.makedirs(DATA_DIR, exist_ok=True)

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Cache-Control":   "no-cache, no-store, must-revalidate",
    "Pragma":          "no-cache",
    "Expires":         "0",
}

# Shared session — keep-alive reuses existing TCP connections instead of
# opening a new one (and burning an ephemeral port) on every poll cycle.
# Pool limits prevent socket buffer exhaustion on Windows (WinError 10055).
from requests.adapters import HTTPAdapter
_HTTP = requests.Session()
_HTTP.headers.update(HEADERS)
_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=0)
_HTTP.mount("https://", _adapter)
_HTTP.mount("http://", _adapter)


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


def load_products_cache() -> dict:
    if os.path.exists(PRODUCTS_CACHE_FILE):
        with open(PRODUCTS_CACHE_FILE) as f:
            return json.load(f)
    return {}


def save_products_cache(cache: dict):
    with open(PRODUCTS_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


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

def _base_url(url: str) -> str:
    """Strip path/query from a store URL down to https://domain."""
    from urllib.parse import urlparse
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _fetch_paginated_sync(url: str, key: str, delay: float = 0.5) -> tuple[list, bool]:
    """Fetch all pages of a Shopify endpoint.

    Tries cursor-based pagination (Link header) first. If the first response
    has no Link header, falls back to legacy page-based (?page=N) pagination,
    which is required for stores like Gymshark that don't emit Link headers.
    """
    import time
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["limit"] = ["250"]
    qs.pop("page", None)
    qs.pop("page_info", None)
    base_url = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))

    results        = []
    password_locked = False
    session        = _HTTP

    # ── Page 1 ────────────────────────────────────────────────────────────────
    try:
        with session.get(base_url, timeout=15) as r:
            if r.status_code == 429:
                time.sleep(5)
            if r.status_code in (401, 403) or "password" in r.url:
                return [], True
            r.raise_for_status()
            data = r.json()
        if not isinstance(data, dict):
            return [], False
        batch = data.get(key, [])
        results.extend(batch)
        link_header_p1 = r.headers.get("Link", "")
    except Exception as e:
        log.error(f"Failed to fetch page 1 of {url}: {e}")
        return [], False

    # ── Decide pagination strategy from the first response ────────────────────
    next_url    = None
    for part in link_header_p1.split(","):
        if 'rel="next"' in part:
            m = re.search(r"<([^>]+)>", part)
            if m:
                next_url = m.group(1)
            break

    use_page_based = not next_url and len(batch) == 250

    if use_page_based:
        log.debug(f"No Link header on first page — switching to page-based pagination for {url}")

    # ── Cursor-based: follow Link headers ─────────────────────────────────────
    if not use_page_based:
        page_num = 1
        while next_url:
            page_num += 1
            time.sleep(delay)
            try:
                with session.get(next_url, timeout=15) as r:
                    if r.status_code == 429:
                        log.warning(f"Rate limited on page {page_num}, retrying in 5s")
                        time.sleep(5)
                        continue
                    if r.status_code in (401, 403) or "password" in r.url:
                        password_locked = True
                        break
                    r.raise_for_status()
                    data     = r.json()
                    lh       = r.headers.get("Link", "")
                if not isinstance(data, dict):
                    break
                batch = data.get(key, [])
                results.extend(batch)

                next_url = None
                for part in lh.split(","):
                    if 'rel="next"' in part:
                        m = re.search(r"<([^>]+)>", part)
                        if m:
                            next_url = m.group(1)
                        break
            except requests.HTTPError:
                break
            except Exception as e:
                log.error(f"Failed to fetch page {page_num}: {e}")
                break
        log.debug(f"Cursor pagination done: {page_num} page(s), {len(results)} total {key}")

    # ── Page-based: increment ?page= until empty ──────────────────────────────
    else:
        page_num = 1
        qs.pop("page_info", None)
        while True:
            page_num += 1
            qs["page"] = [str(page_num)]
            page_url = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
            time.sleep(delay)
            try:
                with session.get(page_url, timeout=15) as r:
                    if r.status_code == 429:
                        log.warning(f"Rate limited on page {page_num}, retrying in 5s")
                        time.sleep(5)
                        continue
                    if r.status_code in (401, 403) or "password" in r.url:
                        password_locked = True
                        break
                    r.raise_for_status()
                    data = r.json()
                if not isinstance(data, dict):
                    break
                batch = data.get(key, [])
                if not batch:
                    break
                results.extend(batch)
            except requests.HTTPError:
                break
            except Exception as e:
                log.error(f"Failed to fetch page {page_num}: {e}")
                break
        log.debug(f"Page-based pagination done: {page_num - 1} page(s), {len(results)} total {key}")

    return results, password_locked


def _normalize_product_js(p: dict) -> dict:
    """Normalize /products/{handle}.js response to products.json shape."""
    for v in p.get("variants", []):
        if isinstance(v.get("price"), int):
            v["price"] = f"{v['price'] / 100:.2f}"
    raw_images = p.get("images", [])
    if raw_images and isinstance(raw_images[0], str):
        p["images"] = [{"src": ("https:" + img if img.startswith("//") else img)} for img in raw_images]
    return p


def _fetch_watched_handles_sync(base: str, handles: list[str]) -> list:
    """Fetch a list of product handles via /products/{handle}.js. Returns normalized product dicts."""
    products = []
    session = _HTTP
    for handle in handles:
        try:
            with session.get(f"{base}/products/{handle}.js", timeout=10) as r:
                if r.status_code == 404:
                    products.append({"handle": handle, "_removed": True})
                    continue
                if not r.ok:
                    continue
                products.append(_normalize_product_js(r.json()))
        except Exception as e:
            log.error(f"Failed to fetch watched handle {handle}: {e}")
    return products


def _search_suggest_sync(base: str, query: str, limit: int = 10) -> list:
    """
    Query /search/suggest.json for product handles, then fetch each product's
    full variant data via /products/{handle}.js. Returns a list of product dicts
    in the same shape as products.json items.
    """
    session = _HTTP
    try:
        with session.get(
            f"{base}/search/suggest.json",
            params={
                "q": query,
                "resources[type]": "product",
                "resources[limit]": limit,
                "resources[options][unavailable_products]": "show",
                "resources[options][fields]": "title,variants.title,vendor",
            },
            timeout=10,
        ) as r:
            if not r.ok:
                return []
            handles = [
                p["handle"]
                for p in r.json().get("resources", {}).get("results", {}).get("products", [])
                if p.get("handle")
            ]
    except Exception as e:
        log.error(f"suggest failed for {base}: {e}")
        return []

    products = []
    for handle in handles:
        try:
            with session.get(f"{base}/products/{handle}.js", timeout=10) as rp:
                if not rp.ok:
                    continue
                products.append(_normalize_product_js(rp.json()))
        except Exception as e:
            log.error(f"product .js fetch failed for {base}/products/{handle}: {e}")
    return products


async def search_suggest(base: str, query: str) -> list:
    return await asyncio.to_thread(_search_suggest_sync, base, query)


async def fetch_products(url: str) -> tuple[list, bool]:
    """Fetch all products for a store. Returns (products, password_locked)."""
    base = _base_url(url)
    return await asyncio.to_thread(
        _fetch_paginated_sync, f"{base}/products.json", "products", 0.5
    )


def _probe_shopify_sync(url: str) -> bool:
    """Return True if the URL is a valid, reachable Shopify products.json endpoint."""
    for attempt in range(2):
        try:
            with _HTTP.get(url, timeout=20) as r:
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
    candidates = [f"https://{domain}/products.json?limit=250"]

    if domain.startswith("www."):
        bare = domain[4:]
        candidates.append(f"https://secure.{bare}/products.json?limit=250")
    elif domain.startswith("secure."):
        bare = domain[7:]
        candidates.append(f"https://www.{bare}/products.json?limit=250")
    else:
        candidates.append(f"https://www.{domain}/products.json?limit=250")
        candidates.append(f"https://secure.{domain}/products.json?limit=250")

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
    """Return True if a user/role subscription's filters match the given store + variant."""
    if sub.get("stores") and store_name not in sub["stores"]:
        return False
    if sub.get("names"):
        search_text = (variant["title"] + " " + variant["variant_title"]).lower()
        if not all(kw.lower() in search_text for kw in sub["names"]):
            return False
    if sub.get("sizes"):
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
    return "Variants", ", ".join(filtered)


def _product_url(store_url: str, handle: str) -> str:
    base   = store_url.split("?")[0].rstrip("/products.json").rstrip("/")
    scheme, _, domain_path = base.partition("://")
    parts  = domain_path.split("/", 1)
    domain = _display_domain(parts[0])
    path   = "/" + parts[1] if len(parts) > 1 else ""
    return f"{scheme}://{domain}{path}/products/{handle}"


AGGREGATE_THRESHOLD = 20


def _size_list(variants: list, available_only: bool = False) -> str:
    sizes = [v["variant_title"] for v in variants
             if (not available_only or v.get("available", True))
             and v["variant_title"].lower() != "default title"]
    return ", ".join(sizes) if sizes else "—"


def make_aggregate_embed(store_name: str, store_url: str,
                         restocked: dict, new_items: dict) -> discord.Embed:
    domain     = _display_domain(store_url.split("/")[2])
    total      = len(restocked) + len(new_items)
    footer     = f"{bot_footer()} • {domain}"
    EMBED_LIMIT = 5800

    lines = []
    for variants in restocked.values():
        title = variants[0]["title"]
        sizes = _size_list(variants, available_only=False)
        lines.append(f"🟢 **{title}** ({sizes})")
    for variants in new_items.values():
        title = variants[0]["title"]
        sizes = _size_list([v for v in variants if v.get("available")]) or _size_list(variants)
        lines.append(f"🟠 **{title}** ({sizes})")

    title_text = f"📦 Mass Drop: {store_name} — {total} items"
    used  = len(title_text) + len(footer)
    shown = []
    for line in lines:
        if used + len(line) + 1 > EMBED_LIMIT:
            # Too large — return a simple summary embed instead
            embed = discord.Embed(
                title=f"📦 Mass Drop: {store_name}",
                description=f"**{total}** item{'s' if total != 1 else ''} updated — check the store for details.",
                color=0x5865F2,
                timestamp=datetime.now(ZoneInfo("UTC")),
            )
            embed.set_footer(text=footer)
            return embed
        shown.append(line)
        used += len(line) + 1

    embed = discord.Embed(title=title_text, color=0x5865F2, timestamp=datetime.now(ZoneInfo("UTC")))
    embed.set_footer(text=footer)

    chunk, chunks = [], []
    for line in shown:
        if sum(len(l) + 1 for l in chunk) + len(line) > 1000:
            chunks.append(chunk)
            chunk = []
        chunk.append(line)
    if chunk:
        chunks.append(chunk)
    for i, ch in enumerate(chunks):
        embed.add_field(name="Items" if i == 0 else "​", value="\n".join(ch), inline=False)

    return embed


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
    embed.add_field(name="Variants", value=size_lines or "N/A", inline=True)
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


def make_sold_out_embed(store_name: str, store_url: str, sold_out: dict) -> discord.Embed:
    """sold_out: handle → list of variant dicts (same shape as the sold_out dict from the poll loop)."""
    domain = _display_domain(store_url.split("/")[2])
    embed  = discord.Embed(
        title=f"🔴 Sold Out — {store_name}",
        color=0xED4245,
        timestamp=datetime.now(ZoneInfo("UTC")),
    )
    lines = []
    for variants in sold_out.values():
        title = variants[0]["title"]
        _, sizes = _format_sizes([v["variant_title"] for v in variants])
        lines.append(f"**{title}** ({sizes})")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"{bot_footer()} • {domain}")
    return embed


# ── Cog ───────────────────────────────────────────────────────────────────────

DEFAULT_POLL_INTERVAL = 300

def _default_guild() -> dict:
    return {
        "alert_channel_id": None,
        "stores":           {},
        "channels":         {},
        "forum_threads":    {},
        "subscriptions":    [],
        "poll_interval":    DEFAULT_POLL_INTERVAL,
    }


def make_password_embed(store_name: str, store_url: str, locked: bool) -> discord.Embed:
    domain = _display_domain(store_url.split("/")[2])
    if locked:
        embed = discord.Embed(
            title=f"🔒 Store Locked: {store_name}",
            description="The store is now password-protected. This often signals an upcoming drop.",
            color=0xED4245,
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
    else:
        embed = discord.Embed(
            title=f"🔓 Store Live: {store_name}",
            description="The password page is down — the store is accessible again.",
            color=0x57F287,
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
    embed.set_footer(text=f"{bot_footer()} • {domain}")
    return embed


class RestockCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot             = bot
        self.state           = load_state()
        self.products_cache  = load_products_cache()
        raw                  = load_bot_state()
        self.guilds: dict    = load_all_guilds()
        self._last_polled: dict  = {}   # guild_id_str → last poll timestamp
        self.password_state: dict[str, bool] = {}  # store_url → currently locked

        # Detect legacy single-guild format and migrate in on_ready
        if not self.guilds and ("alert_channel_id" in raw or "guilds" in raw):
            self._legacy_state = raw

    async def cog_unload(self):
        self.poll.cancel()
        _HTTP.close()

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
        gs.setdefault("channels", {})
        gs.setdefault("forum_threads", {})
        gs.setdefault("subscriptions", [])
        gs.setdefault("poll_interval", DEFAULT_POLL_INTERVAL)
        gs.setdefault("store_alerts", {})
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

    def _guild_is_active(self, gs: dict) -> bool:
        return bool(gs.get("alert_channel_id") or gs.get("channels"))

    def _min_interval(self) -> int:
        intervals = [
            gs.get("poll_interval", DEFAULT_POLL_INTERVAL)
            for gs in self.guilds.values()
            if self._guild_is_active(gs)
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

    # ── Channel resolution ────────────────────────────────────────────────────

    async def _resolve_store_channel(self, gs: dict, store_name: str, guild_id_str: str = None):
        """
        Return a Messageable to send store alerts to.
        Priority: per-store channel → guild default → None.
        ForumChannels are resolved to a persistent thread named '{store_name} Updates'.
        """
        cid = gs.get("channels", {}).get(store_name) or gs.get("alert_channel_id")
        if not cid:
            return None
        ch = self.bot.get_channel(cid)
        if not ch:
            return None
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            return ch
        if isinstance(ch, discord.ForumChannel):
            return await self._get_or_create_forum_thread(gs, store_name, ch, guild_id_str)
        return None

    async def _get_or_create_forum_thread(self, gs: dict, store_name: str,
                                          forum: discord.ForumChannel, guild_id_str: str = None):
        thread_name = f"{store_name} Updates"
        # Try cached thread id
        cached_id = gs.get("forum_threads", {}).get(store_name)
        if cached_id:
            thread = self.bot.get_channel(cached_id)
            if thread:
                return thread
        # Search active threads in the forum
        for thread in forum.threads:
            if thread.name == thread_name:
                gs.setdefault("forum_threads", {})[store_name] = thread.id
                if guild_id_str:
                    self.persist(guild_id_str)
                return thread
        # Create new thread
        try:
            thread = await forum.create_thread(
                name=thread_name,
                content=f"📋 Alert thread for **{store_name}**. Restocks and new drops will be posted here.",
            )
            # create_thread returns a ThreadWithMessage; grab the thread
            if hasattr(thread, "thread"):
                thread = thread.thread
            gs.setdefault("forum_threads", {})[store_name] = thread.id
            if guild_id_str:
                self.persist(guild_id_str)
            log.info(f"Created forum thread '{thread_name}' in #{forum.name}")
            return thread
        except Exception as e:
            log.error(f"Failed to create forum thread for {store_name}: {e}")
            return None

    def _channel_label(self, gs: dict, store_name: str) -> str:
        """Human-readable description of where a store's alerts go."""
        cid = gs.get("channels", {}).get(store_name)
        default_cid = gs.get("alert_channel_id")
        src_id   = cid or default_cid
        is_default = not cid
        if not src_id:
            return "Not set"
        ch = self.bot.get_channel(src_id)
        if not ch:
            return f"Unknown (`{src_id}`)"
        if isinstance(ch, discord.ForumChannel):
            thread_id = gs.get("forum_threads", {}).get(store_name)
            thread    = self.bot.get_channel(thread_id) if thread_id else None
            post_name = thread.name if thread else f"{store_name} Updates"
            suffix    = " (default)" if is_default else ""
            return f"{ch.mention} › **{post_name}**{suffix}"
        if isinstance(ch, discord.Thread):
            suffix = " (default)" if is_default else ""
            return f"{ch.mention} (thread){suffix}"
        suffix = " (default)" if is_default else ""
        return f"{ch.mention}{suffix}"

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
            if self._guild_is_active(gs) and
               now - self._last_polled.get(gid, 0) >= gs.get("poll_interval", DEFAULT_POLL_INTERVAL)
        }

        if not due_guilds:
            return

        # Collect stores needed by due guilds only, tracking which guilds need each store
        due_stores = {}       # store_name → url
        store_guilds = {}     # store_name → list of guild names
        for gid_str, gs in due_guilds.items():
            guild_obj  = self.bot.get_guild(int(gid_str))
            guild_name = guild_obj.name if guild_obj else gid_str
            for sname, surl in gs.get("stores", {}).items():
                due_stores[sname] = surl
                store_guilds.setdefault(sname, []).append(guild_name)

        # Fetch all stores concurrently (max 5 at a time to avoid socket pressure)
        sem = asyncio.Semaphore(5)
        fetch_results = {}  # store_name → (products, password_locked)

        async def _fetch_store(store_name: str, url: str):
            guild_label = ", ".join(store_guilds.get(store_name, []))
            log.info(f"Checking {store_name} [{guild_label}]...")
            async with sem:
                products, password_locked = await fetch_products(url)
            fetch_results[store_name] = (products, password_locked)

        await asyncio.gather(*[_fetch_store(n, u) for n, u in due_stores.items()])

        for store_name, url in due_stores.items():
            products, password_locked = fetch_results.get(store_name, ([], False))

            # ── Password page detection ───────────────────────────────────────
            was_locked = self.password_state.get(url, False)
            if password_locked != was_locked:
                self.password_state[url] = password_locked
                for gid_str, gs in due_guilds.items():
                    if store_name not in gs.get("stores", {}):
                        continue
                    ch = await self._resolve_store_channel(gs, store_name, gid_str)
                    if ch:
                        await ch.send(embed=make_password_embed(store_name, url, password_locked))
                        log.info(f"{'LOCKED' if password_locked else 'UNLOCKED'}: {store_name} → guild {gid_str}")

            if not products:
                continue

            self.products_cache[url] = products
            current  = build_variant_map(products)

            # Fetch watched handles not already in current
            base = _base_url(url)
            watch_subs = [
                s for gs in self.guilds.values()
                for s in gs.get("subscriptions", [])
                if s.get("type") == "watch" and s.get("store") in gs.get("stores", {})
                and gs["stores"].get(s["store"]) == url
            ]
            watched_handles = list({s["handle"] for s in watch_subs} - {v["handle"] for v in current.values()})
            if watched_handles:
                watched_products = await asyncio.to_thread(_fetch_watched_handles_sync, base, watched_handles)
                removed_handles = set()
                for wp in watched_products:
                    if wp.get("_removed"):
                        removed_handles.add(wp["handle"])
                        continue
                    current.update(build_variant_map([wp]))
                # DM watchers of removed products and delete their watches
                if removed_handles:
                    await self._notify_removed_watches(url, removed_handles)

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
                    new_items.setdefault(handle, []).append({**info, "variant_id": vid})
                elif not previous[vid].get("available", True) and info["available"]:
                    restocked.setdefault(handle, []).append({**info, "variant_id": vid})
                elif previous[vid].get("available", True) and not info["available"]:
                    sold_out.setdefault(handle, []).append(info)

            # Detect fully removed products (variants in previous but not in current)
            for vid, info in previous.items():
                if vid not in current:
                    removed.setdefault(info["handle"], []).append(info)

            self.state[url] = current

            # DM users whose watched variants just restocked
            if restocked and watch_subs:
                await self._dispatch_watch_dms(url, restocked, watch_subs)

            if not restocked and not new_items and not sold_out and not removed:
                continue

            # Route alerts to each due guild that monitors this store
            for guild_id_str, gs in due_guilds.items():
                channel = await self._resolve_store_channel(gs, store_name, guild_id_str)
                if not channel:
                    continue

                # Only alert if this store is in this guild's store list
                if store_name not in gs.get("stores", {}):
                    continue

                def _ping_for(variants_list: list) -> str | None:
                    user_ids, role_ids = set(), set()
                    for sub in gs.get("subscriptions", []):
                        if sub.get("type") not in ("user", "role"):
                            continue
                        for v in variants_list:
                            if _sub_matches(sub, store_name, v):
                                (user_ids if sub["type"] == "user" else role_ids).add(sub["target_id"])
                                break
                    parts = [f"<@{uid}>" for uid in user_ids] + [f"<@&{rid}>" for rid in role_ids]
                    return " ".join(parts) if parts else None

                alerts = gs.get("store_alerts", {}).get(store_name, _default_store_alerts())

                def _alert_enabled(key: str, variants_list: list) -> bool:
                    """True if the alert type is toggled on, OR any subscription matches these variants."""
                    if alerts.get(key, True):
                        return True
                    subs = gs.get("subscriptions", [])
                    return any(
                        _sub_matches(sub, store_name, v)
                        for sub in subs if sub.get("type") in ("user", "role")
                        for v in variants_list
                    )

                try:
                    alert_count = len(restocked) + len(new_items)
                    if alert_count > AGGREGATE_THRESHOLD:
                        all_variants = [v for vlist in list(restocked.values()) + list(new_items.values()) for v in vlist]
                        if _alert_enabled("restock", all_variants) or _alert_enabled("new_item", all_variants):
                            ping = _ping_for(all_variants)
                            await channel.send(content=ping, embed=make_aggregate_embed(store_name, url, restocked, new_items))
                            log.info(f"AGGREGATE ({alert_count} items) @ {store_name} → guild {guild_id_str}")
                    else:
                        for variants in restocked.values():
                            if not _alert_enabled("restock", variants):
                                continue
                            await channel.send(
                                content=_ping_for(variants),
                                embed=make_restock_embed(store_name, url, variants),
                            )
                            log.info(f"RESTOCK: {variants[0]['title']} @ {store_name} → guild {guild_id_str}")

                        for variants in new_items.values():
                            if not _alert_enabled("new_item", variants):
                                continue
                            await channel.send(
                                content=_ping_for(variants),
                                embed=make_new_item_embed(store_name, url, variants),
                            )
                            log.info(f"NEW ITEM: {variants[0]['title']} @ {store_name} → guild {guild_id_str}")

                    if sold_out:
                        all_sold = [v for vlist in sold_out.values() for v in vlist]
                        if _alert_enabled("sold_out", all_sold):
                            await channel.send(embed=make_sold_out_embed(store_name, url, sold_out))
                            log.info(f"SOLD OUT: {len(sold_out)} product(s) @ {store_name} → guild {guild_id_str}")

                    for variants in removed.values():
                        if not _alert_enabled("removed", variants):
                            continue
                        await channel.send(embed=make_removed_embed(store_name, url, variants))
                        log.info(f"REMOVED: {variants[0]['title']} @ {store_name} → guild {guild_id_str}")

                except Exception as e:
                    log.error(f"Failed to send alert for {store_name} → guild {guild_id_str}: {e}")

        save_state(self.state)
        save_products_cache(self.products_cache)

        # Stamp last polled time for all due guilds
        for gid in due_guilds:
            self._last_polled[gid] = now

    async def _notify_removed_watches(self, store_url: str, removed_handles: set[str]):
        """DM watchers of removed products and delete their watch subscriptions."""
        for guild_id_str, gs in self.guilds.items():
            store_name = next((n for n, u in gs.get("stores", {}).items() if u == store_url), None)
            if not store_name:
                continue
            watches_to_remove = []
            for s in gs.get("subscriptions", []):
                if s.get("type") != "watch" or s.get("handle") not in removed_handles:
                    continue
                watches_to_remove.append(s["id"])
                try:
                    user = await self.bot.fetch_user(s["target_id"])
                    await user.send(f"👀 A product you were watching has been removed from **{store_name}**: `{s['handle']}`\nYour watch has been automatically deleted.")
                except discord.Forbidden:
                    log.warning(f"Cannot DM user {s['target_id']} about removed watch")
                except Exception as e:
                    log.error(f"Failed to notify user of removed watch: {e}")
            if watches_to_remove:
                gs["subscriptions"] = [s for s in gs["subscriptions"] if s["id"] not in watches_to_remove]
                self.persist(int(guild_id_str))

    async def _dispatch_watch_dms(self, store_url: str, restocked: dict, watch_subs: list[dict]):
        """DM users whose watched variant IDs appear in the restocked dict."""
        # Build a flat map of variant_id -> variant info for restocked variants
        restocked_vids = {}
        for variants in restocked.values():
            for v in variants:
                restocked_vids[str(v.get("variant_id", ""))] = v

        store_name = None
        for gs in self.guilds.values():
            store_name = next((n for n, u in gs.get("stores", {}).items() if u == store_url), None)
            if store_name:
                break

        for sub in watch_subs:
            matched = [restocked_vids[vid] for vid in sub.get("variant_ids", []) if vid in restocked_vids]
            if not matched:
                continue
            try:
                user = await self.bot.fetch_user(sub["target_id"])
                embed = make_restock_embed(store_name or sub["store"], store_url, matched)
                embed.title = f"👀 {embed.title}"
                embed.set_footer(text=f"You're watching this product  •  {bot_footer()}")
                atc = ATCView(store_url, matched)
                await user.send(embed=embed, view=atc if atc.has_buttons else None)
                log.info(f"Watch DM sent to {sub['target_id']} for {sub['handle']}")
            except discord.Forbidden:
                log.warning(f"DMs disabled for user {sub['target_id']}, removing watch {sub['id']}")
                for gid_str, gs in self.guilds.items():
                    before = len(gs.get("subscriptions", []))
                    gs["subscriptions"] = [s for s in gs.get("subscriptions", []) if s["id"] != sub["id"]]
                    if len(gs["subscriptions"]) != before:
                        self.persist(int(gid_str))
            except Exception as e:
                log.error(f"Failed to send watch DM to {sub['target_id']}: {e}")

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    # ── Command groups ────────────────────────────────────────────────────────

    restock   = app_commands.Group(name="rs",        description="Restock monitor commands")
    tracker   = app_commands.Group(name="rst",       description="Restock tracker commands")
    rst_admin = app_commands.Group(
        name="rst-admin",
        description="Admin-only tracker commands",
        default_permissions=discord.Permissions(administrator=True),
    )

    @tracker.command(name="help", description="Show all /rst commands")
    async def tracker_help(self, interaction: discord.Interaction):
        from bot import _build_help_pages, HelpPaginator
        is_admin = interaction.user.guild_permissions.administrator
        pages    = _build_help_pages(is_admin)
        await interaction.response.send_message(embed=pages[1], view=HelpPaginator(pages, 1), ephemeral=True)

    @rst_admin.command(name="help", description="Show all /rst-admin commands")
    async def rst_admin_help(self, interaction: discord.Interaction):
        from bot import _build_help_pages, HelpPaginator
        pages = _build_help_pages(True)
        await interaction.response.send_message(embed=pages[2], view=HelpPaginator(pages, 2), ephemeral=True)

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
        embed.add_field(name="State",    value="🟢 Running" if running else "🔴 Stopped", inline=True)
        embed.add_field(name="Interval", value=f"{interval}s ({interval // 60}m)",        inline=True)
        embed.add_field(name="Default Channel", value=channel.mention if channel else "Not set", inline=True)
        if stores:
            store_lines = "\n".join(
                f"• **{n}** → {self._channel_label(gs, n)}" if n in gs.get("channels", {})
                else f"• {n}"
                for n in stores
            )
        else:
            store_lines = "None — use `/rst-admin add`"
        embed.add_field(name="Stores", value=store_lines, inline=False)
        embed.set_footer(text=bot_footer())
        await interaction.followup.send(embed=embed)

    @tracker.command(name="subscribe", description="Subscribe to restock alerts with optional filters")
    @app_commands.describe(
        store_name="Only notify for this store (leave blank for all stores)",
        names="Comma-separated keywords — item must contain ALL of them (e.g. black,zip-up)",
        sizes="Comma-separated variants — item must match ANY (e.g. small,xs)",
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
        embed.add_field(name="Variants",  value=", ".join(size_list) if size_list else "Any",    inline=False)
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

        subs = [s for s in gs["subscriptions"]
                if s["type"] in ("user", "watch") and s["target_id"] == target.id]

        if not subs:
            await interaction.followup.send(f"No active subscriptions for {target.mention}.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🔔 Subscriptions for {target.display_name}",
            color=0x5865F2,
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
        for s in subs:
            if s["type"] == "watch":
                sizes = ", ".join(s.get("variant_titles", [])) or "All variants"
                lines = [f"👀 **[{s['store']}]** {s['handle']} ({sizes})"]
            else:
                lines = [
                    f"**Store:** {', '.join(s['stores']) if s['stores'] else 'All'}",
                    f"**Names:** {', '.join(s['names']) if s['names'] else 'Any'}",
                    f"**Variants:** {', '.join(s['sizes']) if s['sizes'] else 'Any'}",
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

        relevant = [s for s in gs.get("subscriptions", []) if s.get("type") in ("user", "role") and (not s.get("stores") or store_name in s.get("stores", []))]
        user_subs = [s for s in relevant if s["type"] == "user"]
        role_subs = [s for s in relevant if s["type"] == "role"]

        def _sub_line(s: dict) -> str:
            filters = []
            if s["names"]: filters.append(f"names: {', '.join(s['names'])}")
            if s["sizes"]: filters.append(f"variants: {', '.join(s['sizes'])}")
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
        embed.add_field(name="URL",           value=base_url,                             inline=True)
        embed.add_field(name="Alert Channel", value=self._channel_label(gs, store_name),  inline=True)
        embed.add_field(name=f"👤 Users ({len(user_lines)})", value="\n".join(user_lines) if user_lines else "None", inline=False)
        embed.add_field(name=f"🏷️ Roles ({len(role_lines)})", value="\n".join(role_lines) if role_lines else "None", inline=False)
        embed.set_footer(text=bot_footer())
        view = AlertToggleView(self, interaction.guild_id, store_name)
        await interaction.followup.send(embed=embed, view=view)

    @tracker.command(name="user", description="Show a user's subscriptions")
    @app_commands.describe(user="User to inspect (defaults to you)")
    async def tracker_user(self, interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer()
        gs     = self._guild(interaction.guild_id)
        target = user or interaction.user
        stores = self._guild_stores(interaction.guild_id)

        subs = [s for s in gs.get("subscriptions", []) if s["type"] in ("user", "watch") and s["target_id"] == target.id]

        embed = discord.Embed(title=target.display_name, color=0x5865F2, timestamp=datetime.now(ZoneInfo("UTC")))
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Username", value=str(target), inline=True)

        if subs:
            user_lines = []
            watch_lines = []
            for s in subs:
                if s["type"] == "watch":
                    titles = ", ".join(s.get("variant_titles", [])) or "all variants"
                    watch_lines.append(f"**{s['store']}** — `{s['handle']}` ({titles}) `[{s['id']}]`")
                else:
                    store_label = ", ".join(s["stores"]) if s["stores"] else "All stores"
                    filters = []
                    if s["names"]: filters.append(f"names: {', '.join(s['names'])}")
                    if s["sizes"]: filters.append(f"variants: {', '.join(s['sizes'])}")
                    filter_str = " · ".join(filters) if filters else "all items"
                    user_lines.append(f"**{store_label}** — {filter_str} `[{s['id']}]`")
            if user_lines:
                embed.add_field(name=f"🔔 Subscriptions ({len(user_lines)})", value="\n".join(user_lines), inline=False)
            if watch_lines:
                embed.add_field(name=f"👀 Watches ({len(watch_lines)})", value="\n".join(watch_lines), inline=False)
            if not user_lines and not watch_lines:
                embed.add_field(name="🔔 Subscriptions", value="None", inline=False)
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
        return None, "❌ No alert channel set — run `/rst-admin start` first, or pass a `channel` argument."

    # ── Admin commands (/rst-admin) ──────────────────────────────────────────

    @rst_admin.command(name="start", description="Start monitoring and set the alert channel")
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
        store_list = "\n".join(f"• {name}" for name in stores) or "No stores added yet — use `/rst-admin add`"
        embed = discord.Embed(
            title="🟢 Tracker Started",
            description=f"Alerts → {alert_channel.mention}\n\n**{len(stores)}** store(s) monitored:\n{store_list}",
            color=0x5865F2,
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
        embed.set_footer(text=bot_footer())
        await interaction.followup.send(embed=embed)

    @rst_admin.command(name="channel", description="Set or clear a dedicated alert channel for a store")
    @app_commands.describe(
        store_name="Store to configure",
        channel="Channel, thread, or forum to send alerts to (omit to revert to default)",
    )
    @app_commands.autocomplete(store_name=_store_autocomplete)
    async def admin_channel(self, interaction: discord.Interaction, store_name: str,
                            channel: discord.TextChannel | discord.Thread | discord.ForumChannel = None):
        await interaction.response.defer(ephemeral=True)
        gs     = self._guild(interaction.guild_id)
        stores = self._guild_stores(interaction.guild_id)

        if store_name not in stores:
            await interaction.followup.send(f"❌ **{store_name}** is not a monitored store.", ephemeral=True)
            return

        if channel is None:
            gs["channels"].pop(store_name, None)
            gs["forum_threads"].pop(store_name, None)
            self.persist(interaction.guild_id)
            await interaction.followup.send(
                f"✅ **{store_name}** will now use the default alert channel.", ephemeral=True
            )
            return

        gs["channels"][store_name] = channel.id
        gs["forum_threads"].pop(store_name, None)  # clear cached thread so a fresh one is created
        self.persist(interaction.guild_id)

        if isinstance(channel, discord.ForumChannel):
            ch_type = f"forum — will post to **{store_name} Updates** thread"
        elif isinstance(channel, discord.Thread):
            ch_type = "thread"
        else:
            ch_type = "channel"

        await interaction.followup.send(
            f"✅ **{store_name}** alerts → {channel.mention} ({ch_type})", ephemeral=True
        )

    @rst_admin.command(name="stop", description="Stop monitoring for this server")
    async def admin_stop(self, interaction: discord.Interaction):
        await interaction.response.defer()
        gs = self._guild(interaction.guild_id)
        gs["alert_channel_id"] = None
        self.persist(interaction.guild_id)

        # Stop the loop only if no guild has an active channel
        any_active = any(self._guild_is_active(g) for g in self.guilds.values())
        if not any_active and self.poll.is_running():
            self.poll.cancel()
            await interaction.followup.send("🔴 Tracker stopped (no active servers remaining).")
        else:
            # Recalculate loop interval now this guild is inactive
            new_min = self._min_interval()
            if self.poll.is_running() and self.poll.seconds != new_min:
                self.poll.change_interval(seconds=new_min)
            await interaction.followup.send("🔴 Alerts disabled for this server.")

    @rst_admin.command(name="interval", description="Set this server's poll interval (min 60s, max 600s)")
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

    @rst_admin.command(name="add", description="Add a Shopify store to monitor")
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
        gs.setdefault("store_alerts", {})[store_name] = _default_store_alerts()
        self.persist(interaction.guild_id)
        domain = _display_domain(discovered.split("/")[2])
        await interaction.followup.send(f"✅ Added **{store_name}**\n🔗 `https://{domain}`")

    @rst_admin.command(name="export", description="Export this server's store list as a shareable code")
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
            description=f"Use `/rst-admin import` with the attached code file on another server.\n\n**{len(stores)} store(s):**\n{store_list}",
            color=0x5865F2,
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
        embed.set_footer(text=bot_footer())
        file = discord.File(io.BytesIO(code.encode()), filename="stores-export.txt")
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)

    @rst_admin.command(name="import", description="Import a store list from an export code")
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

    @rst_admin.command(name="remove", description="Remove one or more stores from monitoring")
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

    @rst_admin.command(name="subscribe", description="Create a filtered subscription for a user or role")
    @app_commands.describe(
        target="User or role to subscribe",
        store_name="Only notify for this store (leave blank for all)",
        names="Comma-separated keywords — item must contain ALL of them",
        sizes="Comma-separated variants — item must match ANY",
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
        embed.add_field(name="Variants", value=", ".join(size_list) if size_list else "Any", inline=True)
        if created:
            embed.add_field(name="✅ Created", value="\n".join(f"{t.mention} `[{id}]`" for t, id in created), inline=False)
        if skipped:
            embed.add_field(name="⏭️ Already exists", value="\n".join(f"{t.mention} `[{id}]`" for t, id in skipped), inline=False)
        embed.set_footer(text=bot_footer())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @rst_admin.command(name="unsubscribe", description="Remove any subscription by ID")
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

    @rst_admin.command(name="recent", description="Post the most recently updated item from a store")
    @app_commands.describe(store_name="Store to check", channel="Channel to post in (defaults to tracker channel)")
    @app_commands.autocomplete(store_name=_store_autocomplete)
    async def admin_recent(self, interaction: discord.Interaction,
                           store_name: str, channel: discord.TextChannel = None):
        await interaction.response.defer(ephemeral=True)
        stores = self._guild_stores(interaction.guild_id)

        if store_name not in stores:
            await interaction.followup.send(f"❌ **{store_name}** is not a monitored store.", ephemeral=True)
            return

        if channel:
            dest = channel
        else:
            dest = await self._resolve_store_channel(gs, store_name, str(interaction.guild_id))
        if not dest:
            await interaction.followup.send(
                "❌ No alert channel set for this store — run `/rst-admin start` or `/rst-admin channel`.",
                ephemeral=True,
            )
            return

        products, _ = await fetch_products(stores[store_name])
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

    @rst_admin.command(name="alert", description="Send a fake restock alert to test ping notifications")
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

        if channel:
            dest = channel
        else:
            dest = await self._resolve_store_channel(gs, store_name, str(interaction.guild_id))
        if not dest:
            await interaction.followup.send(
                "❌ No alert channel set for this store — run `/rst-admin start` or `/rst-admin channel`.",
                ephemeral=True,
            )
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

        async def search_store(name: str):
            base     = _base_url(all_stores[name])
            products = await search_suggest(base, query)
            for product in products:
                results.append(SearchResult(name, all_stores[name], product))

        await asyncio.gather(*(search_store(n) for n in chosen))

        store_label = ", ".join(f"**{n}**" for n in chosen)
        if not results:
            await interaction.followup.send(f"No products found matching **{query}** in {store_label}.", ephemeral=True)
            return

        results   = results[:MAX_SEARCH_RESULTS]
        paginator = SearchPaginator(results, cog=self, guild_id=interaction.guild_id)
        await interaction.followup.send(embed=paginator.build_embed(), view=paginator)

    @tracker.command(name="watch", description="Watch a product for restocks — get a DM when your variant drops")
    @app_commands.describe(store_name="Store to search", query="Product name or keyword")
    @app_commands.autocomplete(store_name=_store_autocomplete)
    async def tracker_watch(self, interaction: discord.Interaction, store_name: str, query: str):
        await interaction.response.defer(ephemeral=True)
        stores = self._guild_stores(interaction.guild_id)

        if store_name not in stores:
            await interaction.followup.send(f"❌ **{store_name}** is not a monitored store.", ephemeral=True)
            return

        store_url = stores[store_name]
        base      = _base_url(store_url)
        products  = await search_suggest(base, query)

        if not products:
            await interaction.followup.send(f"No products found matching **{query}** in **{store_name}**.", ephemeral=True)
            return

        if len(products) == 1:
            # Skip the pick list and go straight to size picker
            picker = WatchSizePicker(self, interaction.guild_id, store_name, store_url, products[0])
            await interaction.followup.send(embed=picker.build_embed(), view=picker, ephemeral=True)
        else:
            view = WatchProductSelect(self, interaction.guild_id, store_name, store_url, products)
            await interaction.followup.send("Select a product to watch:", view=view, ephemeral=True)

    @tracker.command(name="catalog", description="Browse all products at a store with stock status")
    @app_commands.describe(store_name="Store to browse")
    @app_commands.autocomplete(store_name=_store_autocomplete)
    async def tracker_catalog(self, interaction: discord.Interaction, store_name: str):
        await interaction.response.defer()
        stores = self._guild_stores(interaction.guild_id)

        if store_name not in stores:
            await interaction.followup.send(f"❌ **{store_name}** is not a monitored store.", ephemeral=True)
            return

        store_url = stores[store_name]
        cached    = self.state.get(store_url)
        if not cached:
            await interaction.followup.send(
                f"❌ No cached data for **{store_name}** yet — wait for the next poll cycle.",
                ephemeral=True,
            )
            return

        # Reconstruct product list from flat variant map grouped by handle
        product_map: dict[str, dict] = {}
        for v in cached.values():
            handle = v.get("handle", "")
            if handle not in product_map:
                product_map[handle] = {
                    "title":    v.get("title", handle),
                    "variants": [],
                }
            product_map[handle]["variants"].append({
                "available": v.get("available", False),
                "price":     v.get("price", "0.00"),
            })

        products = sorted(product_map.values(), key=lambda p: p["title"].lower())
        pages    = [products[i:i + CATALOG_PAGE_SIZE] for i in range(0, len(products), CATALOG_PAGE_SIZE)]
        view     = CatalogPaginator(store_name, store_url, pages)
        await interaction.followup.send(embed=view.build_embed(), view=view)

    @tracker.command(name="check", description="On-demand stock check for a specific product")
    @app_commands.describe(store_name="Store to check", query="Product name or handle")
    @app_commands.autocomplete(store_name=_store_autocomplete)
    async def tracker_check(self, interaction: discord.Interaction, store_name: str, query: str):
        await interaction.response.defer()
        stores = self._guild_stores(interaction.guild_id)

        if store_name not in stores:
            await interaction.followup.send(f"❌ **{store_name}** is not a monitored store.")
            return

        store_url = stores[store_name]
        base      = _base_url(store_url)
        products  = await search_suggest(base, query)

        if not products:
            await interaction.followup.send(f"No products found matching **{query}** in **{store_name}**.")
            return

        results = []
        for product in products[:MAX_SEARCH_RESULTS]:
            results.append(SearchResult(store_name, store_url, product))

        paginator = SearchPaginator(results, cog=self, guild_id=interaction.guild_id)
        await interaction.followup.send(
            content=f"**Live stock check** — {store_name} · `{query}`",
            embed=paginator.build_embed(),
            view=paginator,
        )

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

        # Migrate guilds on old defaults (60s or 180s) up to 300s
        for gid, gs in self.guilds.items():
            if gs.get("poll_interval") in (60, 180):
                gs["poll_interval"] = DEFAULT_POLL_INTERVAL
                self.persist(gid)
                log.info(f"Updated poll_interval for guild {gid} to {DEFAULT_POLL_INTERVAL}s")

        # Force sold_out and removed to disabled on every store for every guild
        for gid, gs in self.guilds.items():
            changed = False
            store_alerts = gs.setdefault("store_alerts", {})
            for store_name in gs.get("stores", {}):
                if store_name not in store_alerts:
                    store_alerts[store_name] = _default_store_alerts()
                    changed = True
                else:
                    sa = store_alerts[store_name]
                    if sa.get("sold_out", True):
                        sa["sold_out"] = False
                        changed = True
                    if sa.get("removed", True):
                        sa["removed"] = False
                        changed = True
            if changed:
                self.persist(gid)
        log.info("Enforced sold_out=False / removed=False defaults across all guilds")

        # Resume poll if any guild has an active channel
        any_active = any(self._guild_is_active(g) for g in self.guilds.values())
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
