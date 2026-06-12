"""Interactive ``discord.ui.View`` components: paginators, pickers, toggles.

Views receive a reference to the cog for state access; it is typed ``Any`` to
avoid a circular import with :mod:`catabot.cog`.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import discord

from . import shopify
from .models import SearchResult
from .storage import bot_footer, save_state

CATALOG_PAGE_SIZE = 15

ALERT_TYPES = [
    ("restock", "🔁 Restock"),
    ("new_item", "🆕 New Item"),
    ("sold_out", "🔴 Sold Out"),
    ("removed", "🗑️ Removed"),
    ("price_change", "💲 Price Change"),
]

DEFAULT_PRICE_CHANGE_THRESHOLD = 0.10  # 10%


def default_store_alerts() -> dict[str, bool]:
    return {
        "restock": True,
        "new_item": True,
        "sold_out": False,
        "removed": False,
        "price_change": False,
    }


def _utcnow() -> datetime:
    return datetime.now(ZoneInfo("UTC"))


class SearchPaginator(discord.ui.View):
    """Paginated search results with a Watch action on the current product."""

    def __init__(self, results: list[SearchResult], cog: Any = None, guild_id: int | None = None):
        super().__init__(timeout=120)
        self.results = results
        self.cog = cog
        self.guild_id = guild_id
        self.page = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page == len(self.results) - 1

    def build_embed(self) -> discord.Embed:
        r = self.results[self.page]
        total = len(self.results)
        embed = discord.Embed(title=r.title, url=r.product_url, color=0x5865F2, timestamp=_utcnow())
        if r.image_url:
            embed.set_thumbnail(url=r.image_url)
        embed.add_field(name="Store", value=r.store_name, inline=True)
        embed.add_field(name="Price", value=r.price, inline=True)
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
        r = self.results[self.page]
        stores = self.cog._guild_stores(self.guild_id)
        store_url = stores.get(r.store_name, "")
        if not store_url:
            await interaction.response.send_message("Could not find store for this product.", ephemeral=True)
            return
        base = shopify.base_url(store_url)
        await interaction.response.defer(ephemeral=True)
        try:
            def _fetch_product():
                resp = shopify.SESSION.get(f"{base}/products/{r.handle}.js", timeout=10)
                return shopify.normalize_product_js(resp.json())

            product = await asyncio.to_thread(_fetch_product)
        except Exception:  # noqa: BLE001
            await interaction.followup.send("Could not fetch product details. Please try again.", ephemeral=True)
            return
        picker = WatchSizePicker(self.cog, self.guild_id, r.store_name, store_url, product)
        await interaction.followup.send(embed=picker.build_embed(), view=picker, ephemeral=True)


class WatchSizePicker(discord.ui.View):
    """Size-picker UI for watching a product. Shows all variants as toggles."""

    def __init__(self, cog: Any, guild_id: int, store_name: str, store_url: str, product: dict,
                 preselect: set[str] | None = None):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        self.store_name = store_name
        self.store_url = store_url

        self.product_title = product.get("title", "Unknown")
        self.handle = product.get("handle", "")
        base = shopify.base_url(store_url)
        self.product_url = f"{base}/products/{self.handle}"
        self.image_url = (product.get("images") or [{}])[0].get("src")

        self.variants: list[dict] = product.get("variants", [])
        self.selected: set[str] = set(preselect or [])

        # One button per variant (cap at 20 to leave a row for Confirm/Cancel).
        for v in self.variants[:20]:
            vid = str(v["id"])
            avail = v.get("available", False)
            btn = discord.ui.Button(
                label=f"{v.get('title', vid)} {'✅' if avail else '🔴'}",
                style=discord.ButtonStyle.primary if vid in self.selected else discord.ButtonStyle.secondary,
                custom_id=f"watch_size_{vid}",
            )
            btn.callback = self._make_toggle(vid)
            self.add_item(btn)

        self.confirm_btn = discord.ui.Button(
            label="Confirm", style=discord.ButtonStyle.success, disabled=len(self.selected) == 0, row=4
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
            for item in self.children:
                if isinstance(item, discord.ui.Button) and item.custom_id == f"watch_size_{vid}":
                    item.style = (
                        discord.ButtonStyle.primary if vid in self.selected else discord.ButtonStyle.secondary
                    )
            self.confirm_btn.disabled = len(self.selected) == 0
            await interaction.response.edit_message(view=self)

        return toggle

    async def _confirm(self, interaction: discord.Interaction):
        selected_variants = [v for v in self.variants if str(v["id"]) in self.selected]
        variant_ids = [str(v["id"]) for v in selected_variants]
        variant_titles = [v.get("title", str(v["id"])) for v in selected_variants]

        gs = self.cog._guild(self.guild_id)
        existing = next(
            (
                s
                for s in gs.get("subscriptions", [])
                if s.get("type") == "watch"
                and s.get("handle") == self.handle
                and s.get("target_id") == interaction.user.id
                and sorted(s.get("variant_ids", [])) == sorted(variant_ids)
            ),
            None,
        )
        if existing:
            await interaction.response.edit_message(
                content=f"You already have an identical watch `[{existing['id']}]`.", view=None, embed=None
            )
            return

        sub = {
            "type": "watch",
            "id": str(uuid.uuid4())[:8],
            "target_id": interaction.user.id,
            "store": self.store_name,
            "handle": self.handle,
            "variant_ids": variant_ids,
            "variant_titles": variant_titles,
        }
        gs.setdefault("subscriptions", []).append(sub)
        self.cog.persist(self.guild_id)

        # Seed state so already-available variants don't immediately re-alert.
        state_key = self.store_url
        if state_key in self.cog.state:
            for v in selected_variants:
                vid = str(v["id"])
                if vid not in self.cog.state[state_key]:
                    self.cog.state[state_key][vid] = {
                        "available": v.get("available", False),
                        "title": self.product_title,
                        "variant_title": v.get("title", ""),
                        "price": str(v.get("price", "0.00")),
                        "handle": self.handle,
                        "image_url": self.image_url,
                    }
            await asyncio.to_thread(save_state, self.cog.state)

        sizes_str = ", ".join(variant_titles)
        await interaction.response.edit_message(
            content=(
                f"👀 Watching **{self.product_title}** ({sizes_str}) at **{self.store_name}**. "
                "You'll get a DM when it restocks."
            ),
            embed=None,
            view=None,
        )

        in_stock = [v for v in selected_variants if v.get("available")]
        if in_stock:
            try:
                sizes_in_stock = ", ".join(v.get("title", "") for v in in_stock)
                await interaction.user.send(
                    f"👀 Heads up — **{sizes_in_stock}** of **{self.product_title}** is already in stock "
                    f"at **{self.store_name}**:\n{self.product_url}"
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
            title=self.product_title,
            url=self.product_url,
            color=0x5865F2,
            description="Select the variants you want to watch, then click **Confirm**.",
        )
        if self.image_url:
            embed.set_thumbnail(url=self.image_url)
        embed.add_field(name="Store", value=self.store_name, inline=True)
        embed.set_footer(text=bot_footer())
        return embed


class WatchProductSelect(discord.ui.View):
    """Product pick-list shown after ``/rst watch`` search results."""

    def __init__(self, cog: Any, guild_id: int, store_name: str, store_url: str, products: list[dict]):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        self.store_name = store_name
        self.store_url = store_url
        self.products = {p["handle"]: p for p in products}

        options = [
            discord.SelectOption(label=p.get("title", p["handle"])[:100], value=p["handle"]) for p in products[:10]
        ]
        select = discord.ui.Select(placeholder="Choose a product…", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        handle = interaction.data["values"][0]
        product = self.products[handle]
        picker = WatchSizePicker(self.cog, self.guild_id, self.store_name, self.store_url, product)
        await interaction.response.edit_message(embed=picker.build_embed(), view=picker)


class WatchOnSoldOutView(discord.ui.View):
    """Persistent Watch button attached to a sold-out alert embed."""

    def __init__(self, cog: Any, guild_id: int, store_name: str, store_url: str,
                 handle: str, sold_out_variant_ids: list[str]):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.store_name = store_name
        self.store_url = store_url
        self.handle = handle
        self.sold_out_variant_ids = sold_out_variant_ids

    @discord.ui.button(label="👀 Watch", style=discord.ButtonStyle.primary)
    async def watch_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        base = shopify.base_url(self.store_url)
        try:
            def _fetch():
                resp = shopify.SESSION.get(f"{base}/products/{self.handle}.js", timeout=10)
                return shopify.normalize_product_js(resp.json())

            product = await asyncio.to_thread(_fetch)
        except Exception:  # noqa: BLE001
            await interaction.followup.send("Could not fetch product details. Please try again.", ephemeral=True)
            return
        picker = WatchSizePicker(
            self.cog, self.guild_id, self.store_name, self.store_url, product,
            preselect={str(v) for v in self.sold_out_variant_ids},
        )
        await interaction.followup.send(embed=picker.build_embed(), view=picker, ephemeral=True)


class CatalogPaginator(discord.ui.View):
    """Paginated full-catalog listing with per-product stock dots."""

    def __init__(self, store_name: str, store_url: str, pages: list[list[dict]]):
        super().__init__(timeout=180)
        self.store_name = store_name
        self.store_url = store_url
        self.pages = pages
        self.page = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page == len(self.pages) - 1

    def build_embed(self) -> discord.Embed:
        items = self.pages[self.page]
        domain = shopify.display_domain(self.store_url.split("/")[2])
        embed = discord.Embed(
            title=f"🛍️ {self.store_name} Catalog", url=f"https://{domain}", color=0x5865F2, timestamp=_utcnow()
        )
        lines = []
        for item in items:
            variants = item["variants"]
            n_avail = sum(1 for v in variants if v.get("available"))
            n_total = len(variants)
            if n_avail == n_total:
                dot = "🟢"
            elif n_avail == 0:
                dot = "🔴"
            else:
                dot = "🟠"
            price = f"${min(float(v['price']) for v in variants):.2f}" if variants else "N/A"
            lines.append(f"{dot} **{item['title']}** — {price}")
        embed.description = "\n".join(lines)
        embed.set_footer(
            text=(
                f"🟢 In Stock  🟠 Partial  🔴 Sold Out  •  "
                f"Page {self.page + 1} of {len(self.pages)}  •  {bot_footer()} • {domain}"
            )
        )
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


class AlertToggleView(discord.ui.View):
    """Per-store alert-type toggles shown on ``/rst store``."""

    def __init__(self, cog: Any, guild_id: int, store_name: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = guild_id
        self.store_name = store_name
        self._rebuild()

    def _alerts(self) -> dict:
        gs = self.cog._guild(self.guild_id)
        store_alerts = gs.setdefault("store_alerts", {})
        if self.store_name not in store_alerts:
            store_alerts[self.store_name] = default_store_alerts()
            self.cog.persist(self.guild_id)
        return store_alerts[self.store_name]

    def _rebuild(self):
        self.clear_items()
        alerts = self._alerts()
        defaults = default_store_alerts()
        for key, label in ALERT_TYPES:
            enabled = alerts.get(key, defaults.get(key, False))
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary,
                custom_id=f"alert_toggle_{key}",
            )
            btn.callback = self._make_callback(key)
            self.add_item(btn)

    def _make_callback(self, key: str):
        async def callback(interaction: discord.Interaction):
            gs = self.cog._guild(self.guild_id)
            store_alerts = gs.setdefault("store_alerts", {})
            if self.store_name not in store_alerts:
                store_alerts[self.store_name] = default_store_alerts()
            alerts = store_alerts[self.store_name]
            defaults = default_store_alerts()
            alerts[key] = not alerts.get(key, defaults.get(key, False))
            self.cog.persist(self.guild_id)
            self._rebuild()
            try:
                await interaction.response.edit_message(view=self)
            except Exception:  # noqa: BLE001
                pass

        return callback


class ATCView(discord.ui.View):
    """Link-button row for Add-to-Cart on restock/new-item alerts."""

    def __init__(self, store_url: str, variants: list[dict]):
        super().__init__(timeout=None)
        p = urlparse(store_url)
        domain = shopify.display_domain(p.netloc)
        available = [v for v in variants if v.get("available") and v.get("variant_id")]
        single = len(available) == 1
        for v in available[:20]:
            label = v.get("variant_title", "")
            if single or not label or label.lower() == "default title":
                label = "Add to Cart"
            self.add_item(
                discord.ui.Button(
                    label=label[:80],
                    url=f"https://{domain}/cart/{v['variant_id']}:1",
                    style=discord.ButtonStyle.link,
                )
            )

    @property
    def has_buttons(self) -> bool:
        return len(self.children) > 0
