"""The ``RestockCog`` — poll loop, alert routing, and all slash commands.

This module wires together the lower layers (:mod:`catabot.shopify`,
:mod:`catabot.storage`, :mod:`catabot.models`, :mod:`catabot.embeds`,
:mod:`catabot.views`). Several names are imported under their historical
underscore aliases so the command/poll logic reads the same as the domain it
models.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .embeds import (
    AGGREGATE_THRESHOLD,
    make_aggregate_embed,
    make_new_item_embed,
    make_price_change_embed,
    make_removed_embed,
    make_restock_embed,
    make_sold_out_embed,
)
from .models import SearchResult
from .models import migrate_notifications as _migrate_notifications
from .models import normalize_size as _normalize_size
from .models import sub_matches as _sub_matches
from .shopify import (
    SESSION as _HTTP,
)
from .shopify import (
    base_url as _base_url,
)
from .shopify import (
    build_variant_map,
    discover_shopify_url,
    fetch_products,
    search_suggest,
)
from .shopify import (
    display_domain as _display_domain,
)
from .shopify import (
    fetch_watched_handles_sync as _fetch_watched_handles_sync,
)
from .storage import (
    bot_footer,
    load_all_guilds,
    load_bot_state,
    load_products_cache,
    load_state,
    save_bot_state,
    save_guild_state,
    save_products_cache,
    save_state,
)
from .views import (
    CATALOG_PAGE_SIZE,
    DEFAULT_PRICE_CHANGE_THRESHOLD,
    AlertToggleView,
    ATCView,
    CatalogPaginator,
    SearchPaginator,
    WatchOnSoldOutView,
    WatchProductSelect,
    WatchSizePicker,
)
from .views import (
    default_store_alerts as _default_store_alerts,
)

log = logging.getLogger(__name__)

MAX_SEARCH_RESULTS = 20

DEFAULT_POLL_INTERVAL = 300


def _default_guild() -> dict:
    return {
        "alert_channel_id": None,
        "stores": {},
        "channels": {},
        "forum_threads": {},
        "subscriptions": [],
        "poll_interval": DEFAULT_POLL_INTERVAL,
        "price_change_threshold": DEFAULT_PRICE_CHANGE_THRESHOLD,
        "aggregate_threshold": AGGREGATE_THRESHOLD,
    }


class RestockCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.state = load_state()
        self.products_cache = load_products_cache()
        raw = load_bot_state()
        self.guilds: dict = load_all_guilds()
        self._last_polled: dict = {}  # guild_id_str → last poll timestamp

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
        gs.setdefault("price_change_threshold", DEFAULT_PRICE_CHANGE_THRESHOLD)
        gs.setdefault("aggregate_threshold", AGGREGATE_THRESHOLD)
        gs.setdefault("store_alerts", {})
        # Ensure price_change is explicitly False for every store that doesn't have it set yet
        for store_name in gs.get("stores", {}):
            sa = gs["store_alerts"].setdefault(store_name, _default_store_alerts())
            sa.setdefault("price_change", False)
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
            gs.get("poll_interval", DEFAULT_POLL_INTERVAL) for gs in self.guilds.values() if self._guild_is_active(gs)
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

    async def _get_or_create_forum_thread(
        self, gs: dict, store_name: str, forum: discord.ForumChannel, guild_id_str: str = None
    ):
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
        src_id = cid or default_cid
        is_default = not cid
        if not src_id:
            return "Not set"
        ch = self.bot.get_channel(src_id)
        if not ch:
            return f"Unknown (`{src_id}`)"
        if isinstance(ch, discord.ForumChannel):
            thread_id = gs.get("forum_threads", {}).get(store_name)
            thread = self.bot.get_channel(thread_id) if thread_id else None
            post_name = thread.name if thread else f"{store_name} Updates"
            suffix = " (default)" if is_default else ""
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

        now = datetime.now(ZoneInfo("UTC")).timestamp()

        # Determine which guilds are due for a poll this cycle
        due_guilds = {
            gid: gs
            for gid, gs in self.guilds.items()
            if self._guild_is_active(gs)
            and now - self._last_polled.get(gid, 0) >= gs.get("poll_interval", DEFAULT_POLL_INTERVAL)
        }

        if not due_guilds:
            return

        # Collect stores needed by due guilds only, tracking which guilds need each store
        due_stores = {}  # store_name → url
        store_guilds = {}  # store_name → list of guild names
        for gid_str, gs in due_guilds.items():
            guild_obj = self.bot.get_guild(int(gid_str))
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
            products, _ = fetch_results.get(store_name, ([], False))

            if not products:
                continue

            self.products_cache[url] = products
            current = build_variant_map(products)

            # Fetch watched handles not already in current
            base = _base_url(url)
            watch_subs = [
                s
                for gs in self.guilds.values()
                for s in gs.get("subscriptions", [])
                if s.get("type") == "watch"
                and s.get("store") in gs.get("stores", {})
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

            # Guard against partial fetches corrupting state (e.g. mid-fetch socket error)
            if previous and len(current) < len(previous) * 0.7:
                log.warning(
                    f"Skipping state update for {store_name}: got {len(current)} variants "
                    f"vs {len(previous)} previously — likely partial fetch"
                )
                continue

            # Cold-start: seed silently, no alerts
            if previous is None:
                self.state[url] = current
                log.info(f"Seeded {store_name} ({len(current)} variants)")
                continue

            restocked, new_items, sold_out, removed, price_changed = {}, {}, {}, {}, {}
            for vid, info in current.items():
                handle = info["handle"]
                if vid not in previous:
                    new_items.setdefault(handle, []).append({**info, "variant_id": vid})
                elif not previous[vid].get("available", True) and info["available"]:
                    restocked.setdefault(handle, []).append({**info, "variant_id": vid})
                elif previous[vid].get("available", True) and not info["available"]:
                    sold_out.setdefault(handle, []).append({**info, "variant_id": vid})

            # Detect fully removed products (variants in previous but not in current)
            for vid, info in previous.items():
                if vid not in current:
                    removed.setdefault(info["handle"], []).append(info)

            # Detect price changes (per-guild threshold applied later)
            for vid, info in current.items():
                if vid not in previous:
                    continue
                try:
                    old_p = float(previous[vid]["price"])
                    new_p = float(info["price"])
                except (ValueError, KeyError):
                    continue
                if old_p > 0 and old_p != new_p:
                    price_changed.setdefault(info["handle"], []).append(
                        {
                            **info,
                            "variant_id": vid,
                            "old_price": previous[vid]["price"],
                        }
                    )

            self.state[url] = current

            # DM users whose watched variants just restocked
            if restocked and watch_subs:
                await self._dispatch_watch_dms(url, restocked, watch_subs)

            if not restocked and not new_items and not sold_out and not removed and not price_changed:
                continue

            # Route alerts to each due guild that monitors this store
            for guild_id_str, gs in due_guilds.items():
                channel = await self._resolve_store_channel(gs, store_name, guild_id_str)
                if not channel:
                    continue

                # Only alert if this store is in this guild's store list
                if store_name not in gs.get("stores", {}):
                    continue

                # gs/store_name/alerts bound as defaults: these closures are used
                # only within this loop iteration, but binding avoids B023.
                def _ping_for(variants_list: list, gs=gs, store_name=store_name) -> str | None:
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

                def _alert_enabled(key: str, variants_list: list, gs=gs, store_name=store_name, alerts=alerts) -> bool:
                    """True if the alert type is toggled on, OR any subscription matches these variants."""
                    if alerts.get(key, _default_store_alerts().get(key, False)):
                        return True
                    subs = gs.get("subscriptions", [])
                    return any(
                        _sub_matches(sub, store_name, v)
                        for sub in subs
                        if sub.get("type") in ("user", "role")
                        for v in variants_list
                    )

                try:
                    alert_count = len(restocked) + len(new_items)
                    agg_threshold = gs.get("aggregate_threshold", AGGREGATE_THRESHOLD)
                    if alert_count > agg_threshold:
                        all_variants = [
                            v for vlist in list(restocked.values()) + list(new_items.values()) for v in vlist
                        ]
                        if _alert_enabled("restock", all_variants) or _alert_enabled("new_item", all_variants):
                            ping = _ping_for(all_variants)
                            await channel.send(
                                content=ping, embed=make_aggregate_embed(store_name, url, restocked, new_items)
                            )
                            log.info(f"AGGREGATE ({alert_count} items) @ {store_name} → guild {guild_id_str}")
                    else:
                        for variants in restocked.values():
                            if not _alert_enabled("restock", variants):
                                continue
                            atc = ATCView(url, variants)
                            await channel.send(
                                content=_ping_for(variants),
                                embed=make_restock_embed(store_name, url, variants),
                                view=atc if atc.has_buttons else None,
                            )
                            log.info(f"RESTOCK: {variants[0]['title']} @ {store_name} → guild {guild_id_str}")

                        for variants in new_items.values():
                            if not _alert_enabled("new_item", variants):
                                continue
                            atc = ATCView(url, variants)
                            await channel.send(
                                content=_ping_for(variants),
                                embed=make_new_item_embed(store_name, url, variants),
                                view=atc if atc.has_buttons else None,
                            )
                            log.info(f"NEW ITEM: {variants[0]['title']} @ {store_name} → guild {guild_id_str}")

                    if sold_out:
                        all_sold = [v for vlist in sold_out.values() for v in vlist]
                        if _alert_enabled("sold_out", all_sold):
                            for variants in sold_out.values():
                                sold_ids = [str(v.get("variant_id", "")) for v in variants if v.get("variant_id")]
                                watch_view = WatchOnSoldOutView(
                                    self,
                                    int(guild_id_str),
                                    store_name,
                                    url,
                                    variants[0]["handle"],
                                    sold_ids,
                                )
                                await channel.send(
                                    embed=make_sold_out_embed(store_name, url, variants),
                                    view=watch_view,
                                )
                            log.info(f"SOLD OUT: {len(sold_out)} product(s) @ {store_name} → guild {guild_id_str}")

                    for variants in removed.values():
                        if not _alert_enabled("removed", variants):
                            continue
                        await channel.send(embed=make_removed_embed(store_name, url, variants))
                        log.info(f"REMOVED: {variants[0]['title']} @ {store_name} → guild {guild_id_str}")

                    if price_changed:
                        threshold = gs.get("price_change_threshold", DEFAULT_PRICE_CHANGE_THRESHOLD)
                        for variants in price_changed.values():
                            # Filter to variants that meet this guild's threshold
                            qualifying = [
                                v
                                for v in variants
                                if abs(float(v["price"]) - float(v["old_price"])) / float(v["old_price"]) >= threshold
                            ]
                            if not qualifying:
                                continue
                            if not _alert_enabled("price_change", qualifying):
                                continue
                            atc = ATCView(url, qualifying)
                            await channel.send(
                                content=_ping_for(qualifying),
                                embed=make_price_change_embed(store_name, url, qualifying),
                                view=atc if atc.has_buttons else None,
                            )
                            log.info(f"PRICE CHANGE: {qualifying[0]['title']} @ {store_name} → guild {guild_id_str}")

                except Exception as e:
                    log.error(f"Failed to send alert for {store_name} → guild {guild_id_str}: {e}")

        await asyncio.gather(
            asyncio.to_thread(save_state, self.state),
            asyncio.to_thread(save_products_cache, self.products_cache),
        )

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
                    await user.send(
                        f"👀 A product you were watching has been removed from **{store_name}**: `{s['handle']}`\nYour watch has been automatically deleted."
                    )
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

    restock = app_commands.Group(name="rs", description="Restock monitor commands")
    tracker = app_commands.Group(name="rst", description="Restock tracker commands")
    rst_admin = app_commands.Group(
        name="rst-admin",
        description="Admin-only tracker commands",
        default_permissions=discord.Permissions(administrator=True),
    )

    @tracker.command(name="help", description="Show all /rst commands")
    async def tracker_help(self, interaction: discord.Interaction):
        from catabot.app import HelpPaginator, build_help_pages

        is_admin = interaction.user.guild_permissions.administrator
        pages = build_help_pages(is_admin)
        await interaction.response.send_message(embed=pages[1], view=HelpPaginator(pages, 1), ephemeral=True)

    @rst_admin.command(name="help", description="Show all /rst-admin commands")
    async def rst_admin_help(self, interaction: discord.Interaction):
        from catabot.app import HelpPaginator, build_help_pages

        pages = build_help_pages(True)
        await interaction.response.send_message(embed=pages[2], view=HelpPaginator(pages, 2), ephemeral=True)

    async def _store_autocomplete(self, interaction: discord.Interaction, current: str):
        stores = self._guild_stores(interaction.guild_id)
        return [app_commands.Choice(name=n, value=n) for n in stores if current.lower() in n.lower()][:25]

    # ── Public commands (/rst) ────────────────────────────────────────────────

    @tracker.command(name="status", description="Show current tracker status")
    async def tracker_status(self, interaction: discord.Interaction):
        await interaction.response.defer()
        gs = self._guild(interaction.guild_id)
        running = self.poll.is_running()
        stores = self._guild_stores(interaction.guild_id)
        ch_id = gs.get("alert_channel_id")
        channel = self.bot.get_channel(ch_id) if ch_id else None

        embed = discord.Embed(
            title="📊 Tracker Status",
            color=0x57F287 if running else 0xED4245,
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
        interval = gs.get("poll_interval", DEFAULT_POLL_INTERVAL)
        embed.add_field(name="State", value="🟢 Running" if running else "🔴 Stopped", inline=True)
        embed.add_field(name="Interval", value=f"{interval}s ({interval // 60}m)", inline=True)
        embed.add_field(name="Default Channel", value=channel.mention if channel else "Not set", inline=True)
        if stores:
            store_lines = "\n".join(
                f"• **{n}** → {self._channel_label(gs, n)}" if n in gs.get("channels", {}) else f"• {n}" for n in stores
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
    async def tracker_subscribe(
        self, interaction: discord.Interaction, store_name: str = None, names: str = None, sizes: str = None
    ):
        await interaction.response.defer(ephemeral=True)
        gs = self._guild(interaction.guild_id)
        stores = self._guild_stores(interaction.guild_id)

        if store_name and store_name not in stores:
            await interaction.followup.send(f"❌ **{store_name}** is not a monitored store.", ephemeral=True)
            return

        name_list = [k.strip().lower() for k in names.split(",") if k.strip()] if names else []
        size_list = [_normalize_size(s) for s in sizes.split(",") if s.strip()] if sizes else []
        store_list = [store_name] if store_name else []

        duplicate = next(
            (
                s
                for s in gs["subscriptions"]
                if s["type"] == "user"
                and s["target_id"] == interaction.user.id
                and sorted(s["stores"]) == sorted(store_list)
                and sorted(s["names"]) == sorted(name_list)
                and sorted(s["sizes"]) == sorted(size_list)
            ),
            None,
        )
        if duplicate:
            await interaction.followup.send(
                f"You already have an identical subscription `[{duplicate['id']}]`. Use `/rst subscriptions` to view yours.",
                ephemeral=True,
            )
            return

        sub = {
            "id": uuid.uuid4().hex[:8],
            "type": "user",
            "target_id": interaction.user.id,
            "stores": store_list,
            "names": name_list,
            "sizes": size_list,
        }
        gs["subscriptions"].append(sub)
        self.persist(interaction.guild_id)

        embed = discord.Embed(title="🔔 Subscription Created", color=0x57F287, timestamp=datetime.now(ZoneInfo("UTC")))
        embed.add_field(name="ID", value=f"`{sub['id']}`", inline=True)
        embed.add_field(name="Store", value=store_name or "All stores", inline=True)
        embed.add_field(name="Names", value=", ".join(name_list) if name_list else "Any", inline=False)
        embed.add_field(name="Variants", value=", ".join(size_list) if size_list else "Any", inline=False)
        embed.set_footer(text=f"Use /rst unsubscribe {sub['id']} to remove  •  {bot_footer()}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tracker.command(name="unsubscribe", description="Remove one of your subscriptions by ID")
    @app_commands.describe(sub_id="Subscription ID shown in /rst subscriptions")
    async def tracker_unsubscribe(self, interaction: discord.Interaction, sub_id: str):
        await interaction.response.defer(ephemeral=True)
        gs = self._guild(interaction.guild_id)
        is_admin = interaction.user.guild_permissions.administrator

        before = len(gs["subscriptions"])
        gs["subscriptions"] = [
            s
            for s in gs["subscriptions"]
            if not (s["id"] == sub_id and (is_admin or (s["type"] == "user" and s["target_id"] == interaction.user.id)))
        ]

        if len(gs["subscriptions"]) == before:
            await interaction.followup.send(
                f"❌ No subscription with ID `{sub_id}` found (or it's not yours).", ephemeral=True
            )
            return

        self.persist(interaction.guild_id)
        await interaction.followup.send(f"✅ Removed subscription `{sub_id}`.", ephemeral=True)

    @tracker.command(name="subscriptions", description="List your active subscriptions")
    @app_commands.describe(user="User to inspect (admin only; defaults to you)")
    async def tracker_subscriptions(self, interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer(ephemeral=True)
        gs = self._guild(interaction.guild_id)
        target = user or interaction.user

        if user and user != interaction.user and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("❌ Only admins can view other users' subscriptions.", ephemeral=True)
            return

        subs = [s for s in gs["subscriptions"] if s["type"] in ("user", "watch") and s["target_id"] == target.id]

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
        gs = self._guild(interaction.guild_id)
        stores = self._guild_stores(interaction.guild_id)

        if store_name not in stores:
            await interaction.followup.send(f"❌ **{store_name}** is not a monitored store.")
            return

        store_url = stores[store_name]
        domain = _display_domain(store_url.split("/")[2])
        base_url = f"https://{domain}"

        relevant = [
            s
            for s in gs.get("subscriptions", [])
            if s.get("type") in ("user", "role") and (not s.get("stores") or store_name in s.get("stores", []))
        ]
        user_subs = [s for s in relevant if s["type"] == "user"]
        role_subs = [s for s in relevant if s["type"] == "role"]

        def _sub_line(s: dict) -> str:
            filters = []
            if s["names"]:
                filters.append(f"names: {', '.join(s['names'])}")
            if s["sizes"]:
                filters.append(f"variants: {', '.join(s['sizes'])}")
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

        embed = discord.Embed(
            title=f"🏪 {store_name}", url=base_url, color=0x5865F2, timestamp=datetime.now(ZoneInfo("UTC"))
        )
        embed.add_field(name="URL", value=base_url, inline=True)
        embed.add_field(name="Alert Channel", value=self._channel_label(gs, store_name), inline=True)
        embed.add_field(
            name=f"👤 Users ({len(user_lines)})", value="\n".join(user_lines) if user_lines else "None", inline=False
        )
        embed.add_field(
            name=f"🏷️ Roles ({len(role_lines)})", value="\n".join(role_lines) if role_lines else "None", inline=False
        )
        embed.set_footer(text=bot_footer())
        view = AlertToggleView(self, interaction.guild_id, store_name)
        await interaction.followup.send(embed=embed, view=view)

    @tracker.command(name="user", description="Show a user's subscriptions")
    @app_commands.describe(user="User to inspect (defaults to you)")
    async def tracker_user(self, interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer()
        gs = self._guild(interaction.guild_id)
        target = user or interaction.user

        subs = [
            s for s in gs.get("subscriptions", []) if s["type"] in ("user", "watch") and s["target_id"] == target.id
        ]

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
                    if s["names"]:
                        filters.append(f"names: {', '.join(s['names'])}")
                    if s["sizes"]:
                        filters.append(f"variants: {', '.join(s['sizes'])}")
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
        gs = self._guild(interaction.guild_id)
        alert_channel = channel or interaction.channel
        gs["alert_channel_id"] = alert_channel.id

        if "stores" not in gs:
            gs["stores"] = {}

        self.persist(interaction.guild_id)

        if not self.poll.is_running():
            self.poll.start()

        stores = self._guild_stores(interaction.guild_id)
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
    async def admin_channel(
        self,
        interaction: discord.Interaction,
        store_name: str,
        channel: discord.TextChannel | discord.Thread | discord.ForumChannel = None,
    ):
        await interaction.response.defer(ephemeral=True)
        gs = self._guild(interaction.guild_id)
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

        await interaction.followup.send(f"✅ **{store_name}** alerts → {channel.mention} ({ch_type})", ephemeral=True)

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

        await interaction.followup.send(
            f"✅ Poll interval for this server updated to **{seconds}s** ({seconds // 60}m {seconds % 60}s)."
        )

    @rst_admin.command(
        name="price_threshold", description="Set minimum price change % to trigger a price alert (default 10%)"
    )
    @app_commands.describe(percent="Minimum % change required to send an alert (1–100, e.g. 10 = 10%)")
    async def admin_price_threshold(self, interaction: discord.Interaction, percent: int):
        if not 1 <= percent <= 100:
            await interaction.response.send_message("❌ Value must be between **1** and **100**.", ephemeral=True)
            return
        gs = self._guild(interaction.guild_id)
        gs["price_change_threshold"] = percent / 100
        self.persist(interaction.guild_id)
        await interaction.response.send_message(
            f"✅ Price change threshold set to **{percent}%**. Enable price alerts per store via `/rst store`.",
            ephemeral=True,
        )

    @rst_admin.command(
        name="aggregate_threshold",
        description="Set how many changed products trigger a mass-drop summary instead of individual alerts (default 20)",
    )
    @app_commands.describe(count="Product count, e.g. 200 for large stores. Use 9999 to disable aggregation.")
    async def admin_aggregate_threshold(self, interaction: discord.Interaction, count: int):
        if count < 1:
            await interaction.response.send_message("❌ Value must be at least **1**.", ephemeral=True)
            return
        gs = self._guild(interaction.guild_id)
        gs["aggregate_threshold"] = count
        self.persist(interaction.guild_id)
        await interaction.response.send_message(
            f"✅ Aggregate threshold set to **{count}** products. "
            f"Polls with ≤ {count} changed items will send individual alerts.",
            ephemeral=True,
        )

    @rst_admin.command(name="add", description="Add a Shopify store to monitor")
    @app_commands.describe(
        store_name="Display name for the store", url="Store URL (e.g. https://www.houndarchives.com)"
    )
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
        gs = self._guild(interaction.guild_id)
        stores = gs.get("stores", {})

        if not stores:
            await interaction.followup.send("❌ No stores to export.", ephemeral=True)
            return

        import base64
        import io

        code = base64.urlsafe_b64encode(json.dumps(stores).encode()).decode()
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
            padded += "=" * (-len(padded) % 4)
            stores = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
            if not isinstance(stores, dict):
                raise ValueError
        except Exception:
            await interaction.followup.send("❌ Invalid code.", ephemeral=True)
            return

        gs = self._guild(interaction.guild_id)
        existing = gs.get("stores", {})
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
        if added:
            lines.append(f"✅ Imported {len(added)} store(s): " + ", ".join(f"**{n}**" for n in added))
        if skipped:
            lines.append(
                f"⏭️ Skipped {len(skipped)} (already present by name or URL): " + ", ".join(f"**{n}**" for n in skipped)
            )
        await interaction.followup.send("\n".join(lines) or "No stores imported.", ephemeral=True)

    @rst_admin.command(name="remove", description="Remove one or more stores from monitoring")
    @app_commands.describe(
        store1="Store to remove",
        store2="Additional store",
        store3="Additional store",
        store4="Additional store",
        store5="Additional store",
    )
    @app_commands.autocomplete(
        store1=_store_autocomplete,
        store2=_store_autocomplete,
        store3=_store_autocomplete,
        store4=_store_autocomplete,
        store5=_store_autocomplete,
    )
    async def admin_remove(
        self,
        interaction: discord.Interaction,
        store1: str,
        store2: str = None,
        store3: str = None,
        store4: str = None,
        store5: str = None,
    ):
        await interaction.response.defer()
        gs = self._guild(interaction.guild_id)
        names = [s for s in [store1, store2, store3, store4, store5] if s]
        removed, not_found = [], []

        for name in names:
            if name in gs["stores"]:
                del gs["stores"][name]
                removed.append(name)
            else:
                not_found.append(name)

        if removed:
            self.persist(interaction.guild_id)

        lines = []
        if removed:
            lines.append("✅ Removed: " + ", ".join(f"**{n}**" for n in removed))
        if not_found:
            lines.append("❌ Not found: " + ", ".join(f"**{n}**" for n in not_found))
        await interaction.followup.send("\n".join(lines) or "No changes made.")

    @rst_admin.command(name="subscribe", description="Create a filtered subscription for a user or role")
    @app_commands.describe(
        target="User or role to subscribe",
        store_name="Only notify for this store (leave blank for all)",
        names="Comma-separated keywords — item must contain ALL of them",
        sizes="Comma-separated variants — item must match ANY",
    )
    @app_commands.autocomplete(store_name=_store_autocomplete)
    async def admin_subscribe(
        self,
        interaction: discord.Interaction,
        target: discord.Member | discord.Role,
        store_name: str = None,
        names: str = None,
        sizes: str = None,
    ):
        await interaction.response.defer(ephemeral=True)
        gs = self._guild(interaction.guild_id)

        stores = self._guild_stores(interaction.guild_id)
        if store_name and store_name not in stores:
            await interaction.followup.send(f"❌ **{store_name}** is not a monitored store.", ephemeral=True)
            return

        name_list = [k.strip().lower() for k in names.split(",") if k.strip()] if names else []
        size_list = [_normalize_size(s) for s in sizes.split(",") if s.strip()] if sizes else []
        store_list = [store_name] if store_name else []
        target_type = "role" if isinstance(target, discord.Role) else "user"

        targets = [(target_type, target)]

        created, skipped = [], []
        for target_type, target in targets:
            duplicate = next(
                (
                    s
                    for s in gs["subscriptions"]
                    if s["type"] == target_type
                    and s["target_id"] == target.id
                    and sorted(s["stores"]) == sorted(store_list)
                    and sorted(s["names"]) == sorted(name_list)
                    and sorted(s["sizes"]) == sorted(size_list)
                ),
                None,
            )
            if duplicate:
                skipped.append((target, duplicate["id"]))
                continue
            sub = {
                "id": uuid.uuid4().hex[:8],
                "type": target_type,
                "target_id": target.id,
                "stores": store_list,
                "names": name_list,
                "sizes": size_list,
            }
            gs["subscriptions"].append(sub)
            created.append((target, sub["id"]))

        if created:
            self.persist(interaction.guild_id)

        embed = discord.Embed(title="🔔 Subscription Results", color=0x57F287, timestamp=datetime.now(ZoneInfo("UTC")))
        embed.add_field(name="Store", value=store_name or "All stores", inline=True)
        embed.add_field(name="Names", value=", ".join(name_list) if name_list else "Any", inline=True)
        embed.add_field(name="Variants", value=", ".join(size_list) if size_list else "Any", inline=True)
        if created:
            embed.add_field(
                name="✅ Created", value="\n".join(f"{t.mention} `[{id}]`" for t, id in created), inline=False
            )
        if skipped:
            embed.add_field(
                name="⏭️ Already exists", value="\n".join(f"{t.mention} `[{id}]`" for t, id in skipped), inline=False
            )
        embed.set_footer(text=bot_footer())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @rst_admin.command(name="unsubscribe", description="Remove any subscription by ID")
    @app_commands.describe(sub_id="Subscription ID to remove")
    async def admin_unsubscribe(self, interaction: discord.Interaction, sub_id: str):
        await interaction.response.defer(ephemeral=True)
        gs = self._guild(interaction.guild_id)
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
    async def admin_recent(
        self, interaction: discord.Interaction, store_name: str, channel: discord.TextChannel = None
    ):
        await interaction.response.defer(ephemeral=True)
        gs = self._guild(interaction.guild_id)
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

        latest = max(products, key=lambda p: p.get("updated_at", ""))
        images = latest.get("images", [])
        image_url = images[0]["src"] if images else None
        variants = latest.get("variants", [])
        available = [v for v in variants if v.get("available")]
        unavailable = [v for v in variants if not v.get("available")]
        price = f"${float(variants[0]['price']):.2f}" if variants else "N/A"
        store_url = stores[store_name]
        from urllib.parse import urlparse as _urlparse

        _p = _urlparse(store_url)
        product_url = f"{_p.scheme}://{_display_domain(_p.netloc)}/products/{latest.get('handle', '')}"
        updated_raw = latest.get("updated_at", "")

        embed = discord.Embed(
            title=f"🕐 Most Recent: {latest.get('title', 'Unknown')}",
            url=product_url,
            color=0x5865F2,
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
        if image_url:
            embed.set_thumbnail(url=image_url)
        embed.add_field(name="Store", value=store_name, inline=True)
        embed.add_field(name="Price", value=price, inline=True)
        if available:
            embed.add_field(
                name=f"✅ In Stock ({len(available)})",
                value=", ".join(v.get("title", "") for v in available) or "—",
                inline=False,
            )
        if unavailable:
            embed.add_field(
                name=f"❌ Out of Stock ({len(unavailable)})",
                value=", ".join(v.get("title", "") for v in unavailable) or "—",
                inline=False,
            )
        if updated_raw:
            embed.add_field(
                name="Last Updated",
                value=f"<t:{int(datetime.fromisoformat(updated_raw.replace('Z', '+00:00')).timestamp())}:R>",
                inline=False,
            )
        embed.set_footer(text=f"{bot_footer()} • {_display_domain(store_url.split('/')[2])}")

        await dest.send(embed=embed)
        await interaction.followup.send(
            f"✅ Posted most recent item from **{store_name}** to {dest.mention}.", ephemeral=True
        )

    @rst_admin.command(name="alert", description="Send a fake restock alert to test ping notifications")
    @app_commands.describe(store_name="Store to simulate", channel="Channel to post in (defaults to tracker channel)")
    @app_commands.autocomplete(store_name=_store_autocomplete)
    async def admin_alert(self, interaction: discord.Interaction, store_name: str, channel: discord.TextChannel = None):
        await interaction.response.defer(ephemeral=True)
        gs = self._guild(interaction.guild_id)
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

        store_url = stores[store_name]
        fake_variants = [
            {
                "title": "Debug Product",
                "variant_title": "M",
                "price": "99.99",
                "handle": "debug-product",
                "image_url": None,
                "available": True,
            }
        ]

        user_ids, role_ids = set(), set()
        for sub in gs.get("subscriptions", []):
            for v in fake_variants:
                if _sub_matches(sub, store_name, v):
                    (user_ids if sub["type"] == "user" else role_ids).add(sub["target_id"])
                    break
        pings = [f"<@{uid}>" for uid in user_ids] + [f"<@&{rid}>" for rid in role_ids]
        ping = " ".join(pings) if pings else None

        embed = make_restock_embed(store_name, store_url, fake_variants)
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
    @app_commands.describe(
        query="Product name or keyword",
        store1="Store to search",
        store2="Additional store",
        store3="Additional store",
        store4="Additional store",
        store5="Additional store",
    )
    @app_commands.autocomplete(
        store1=_store_autocomplete,
        store2=_store_autocomplete,
        store3=_store_autocomplete,
        store4=_store_autocomplete,
        store5=_store_autocomplete,
    )
    async def restock_search(
        self,
        interaction: discord.Interaction,
        query: str,
        store1: str,
        store2: str = None,
        store3: str = None,
        store4: str = None,
        store5: str = None,
    ):
        await interaction.response.defer()
        all_stores = self._guild_stores(interaction.guild_id)
        chosen = [s for s in [store1, store2, store3, store4, store5] if s]
        invalid = [s for s in chosen if s not in all_stores]
        if invalid:
            await interaction.followup.send(
                f"❌ Unknown stores: {', '.join(f'**{s}**' for s in invalid)}", ephemeral=True
            )
            return

        results: list[SearchResult] = []

        async def search_store(name: str):
            base = _base_url(all_stores[name])
            products = await search_suggest(base, query)
            for product in products:
                results.append(SearchResult(name, all_stores[name], product))

        await asyncio.gather(*(search_store(n) for n in chosen))

        store_label = ", ".join(f"**{n}**" for n in chosen)
        if not results:
            await interaction.followup.send(f"No products found matching **{query}** in {store_label}.", ephemeral=True)
            return

        results = results[:MAX_SEARCH_RESULTS]
        paginator = SearchPaginator(results, cog=self, guild_id=interaction.guild_id)
        content = f"**Live stock check** — {chosen[0]} · `{query}`" if len(chosen) == 1 else None
        await interaction.followup.send(content=content, embed=paginator.build_embed(), view=paginator)

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
        base = _base_url(store_url)
        products = await search_suggest(base, query)

        if not products:
            await interaction.followup.send(
                f"No products found matching **{query}** in **{store_name}**.", ephemeral=True
            )
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
        cached = self.state.get(store_url)
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
                    "title": v.get("title", handle),
                    "variants": [],
                }
            product_map[handle]["variants"].append(
                {
                    "available": v.get("available", False),
                    "price": v.get("price", "0.00"),
                }
            )

        products = sorted(product_map.values(), key=lambda p: p["title"].lower())
        pages = [products[i : i + CATALOG_PAGE_SIZE] for i in range(0, len(products), CATALOG_PAGE_SIZE)]
        view = CatalogPaginator(store_name, store_url, pages)
        await interaction.followup.send(embed=view.build_embed(), view=view)

    # ── Startup ───────────────────────────────────────────────────────────────

    def _purge_user(self, uid: int, guild_id: str):
        """Remove all references to a user from a guild's data."""
        gs = self.guilds.get(guild_id)
        if not gs:
            return
        before = len(gs.get("subscriptions", []))
        gs["subscriptions"] = [
            s for s in gs.get("subscriptions", []) if not (s["type"] == "user" and s["target_id"] == uid)
        ]
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
            legacy = self._legacy_state
            guild_id = str(self.bot.guilds[0].id)

            # Format A: flat single-guild (alert_channel_id at root)
            if legacy.get("alert_channel_id") and not self.guilds:
                extra = legacy.get("extra_stores", {})
                self.guilds[guild_id] = {
                    "alert_channel_id": legacy["alert_channel_id"],
                    "stores": extra,
                    "notifications": legacy.get("notifications", {}),
                    "poll_interval": legacy.get("poll_interval", DEFAULT_POLL_INTERVAL),
                }
                self.persist(guild_id)
                log.info(f"Migrated legacy (flat) bot state to guild {guild_id}")

            # Format B: guilds nested dict in bot_state.json
            elif "guilds" in legacy and not self.guilds:
                for gid, gs in legacy["guilds"].items():
                    extra = gs.get("extra_stores", gs.get("stores", {}))
                    self.guilds[gid] = {
                        "alert_channel_id": gs.get("alert_channel_id"),
                        "stores": extra,
                        "notifications": gs.get("notifications", {}),
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

        # Resume poll if any guild has an active channel
        any_active = any(self._guild_is_active(g) for g in self.guilds.values())
        if any_active and not self.poll.is_running():
            self.poll.start()

        # Edit restart confirmation message if present
        raw = load_bot_state()
        restart_channel_id = raw.pop("restart_channel_id", None)
        restart_message_id = raw.pop("restart_message_id", None)
        restart_time = raw.pop("restart_time", 0)
        if restart_channel_id or restart_message_id:
            save_bot_state(raw)

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
