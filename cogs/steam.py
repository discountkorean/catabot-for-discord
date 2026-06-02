import discord
from discord import app_commands
from discord.ext import commands, tasks
import requests
import asyncio
import json
import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_RESULTS      = 5
BASE_DIR         = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WISHLIST_FILE    = os.path.join(BASE_DIR, "data", "steam_wishlists.json")


# ── Wishlist persistence ──────────────────────────────────────────────────────

def load_wishlists() -> dict:
    if os.path.exists(WISHLIST_FILE):
        with open(WISHLIST_FILE) as f:
            return json.load(f)
    return {}


def save_wishlists(data: dict):
    os.makedirs(os.path.dirname(WISHLIST_FILE), exist_ok=True)
    with open(WISHLIST_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── API helpers ───────────────────────────────────────────────────────────────

def _search_sync(query: str) -> list[dict]:
    try:
        r = requests.get(
            "https://store.steampowered.com/api/storesearch/",
            params={"term": query, "l": "english", "cc": "AU"},
            headers=HEADERS, timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        return [i for i in items if i.get("type") == "app"][:MAX_RESULTS]
    except Exception as e:
        log.error(f"Steam search failed: {e}")
        return []


def _appdetails_sync(appid: int) -> dict | None:
    try:
        r = requests.get(
            "https://store.steampowered.com/api/appdetails",
            params={"appids": appid, "cc": "AU", "l": "english"},
            headers=HEADERS, timeout=10,
        )
        r.raise_for_status()
        data = r.json().get(str(appid), {})
        if not data.get("success"):
            return None
        return data.get("data")
    except Exception as e:
        log.error(f"Steam appdetails failed for {appid}: {e}")
        return None


async def fetch_search(query: str) -> list[dict]:
    return await asyncio.to_thread(_search_sync, query)


async def fetch_appdetails(appid: int) -> dict | None:
    return await asyncio.to_thread(_appdetails_sync, appid)


# ── Embed builder ─────────────────────────────────────────────────────────────

def build_game_embed(detail: dict, page: int, total: int) -> discord.Embed:
    appid       = detail.get("steam_appid")
    name        = detail.get("name", "Unknown")
    description = detail.get("short_description", "No description available.")
    header      = detail.get("header_image", "")
    store_url   = f"https://store.steampowered.com/app/{appid}/"

    price_data = detail.get("price_overview")
    if price_data:
        final    = price_data.get("final_formatted", "N/A")
        discount = price_data.get("discount_percent", 0)
        initial  = price_data.get("initial_formatted", "")
        price_str = f"~~{initial}~~ **{final}** 🏷️ -{discount}% OFF" if discount > 0 else final
    elif detail.get("is_free"):
        price_str = "**Free to Play**"
    else:
        price_str = "N/A"

    metacritic = detail.get("metacritic", {})
    meta_score = metacritic.get("score") if metacritic else None
    release    = detail.get("release_date", {})
    rel_date   = release.get("date", "Unknown") if release else "Unknown"
    platforms  = detail.get("platforms", {})
    plat_str   = " ".join(filter(None, [
        "Windows" if platforms.get("windows") else "",
        "Mac"     if platforms.get("mac")     else "",
        "Linux"   if platforms.get("linux")   else "",
    ])) or "Unknown"

    embed = discord.Embed(
        title=name, url=store_url, description=description,
        color=0x1b2838, timestamp=datetime.now(ZoneInfo("UTC")),
    )
    if header:
        embed.set_thumbnail(url=header)

    embed.add_field(name="Price",     value=price_str or "N/A", inline=True)
    embed.add_field(name="Released",  value=rel_date,            inline=True)
    embed.add_field(name="Platforms", value=plat_str,            inline=True)
    if meta_score:
        embed.add_field(name="Metacritic", value=f"{meta_score}/100", inline=True)
    embed.add_field(name="Store Page", value=f"[View on Steam]({store_url})", inline=False)
    embed.set_footer(text=f"Steam  •  Result {page} of {total}")
    return embed


# ── Paginator with Wishlist button ────────────────────────────────────────────

class SteamPaginator(discord.ui.View):
    def __init__(self, details: list[dict]):
        super().__init__(timeout=120)
        self.details    = details
        self.page       = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page == len(self.details) - 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(
            embed=build_game_embed(self.details[self.page], self.page + 1, len(self.details)),
            view=self,
        )

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(
            embed=build_game_embed(self.details[self.page], self.page + 1, len(self.details)),
            view=self,
        )

    @discord.ui.button(label="🔔 Wishlist", style=discord.ButtonStyle.success, row=1)
    async def wishlist_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        detail  = self.details[self.page]
        appid   = str(detail.get("steam_appid"))
        name    = detail.get("name", "Unknown")
        uid     = str(interaction.user.id)

        price_data = detail.get("price_overview", {})
        discount   = price_data.get("discount_percent", 0) if price_data else 0

        wishlists = load_wishlists()
        user_list = wishlists.setdefault(uid, {})

        if appid in user_list:
            await interaction.response.send_message(
                f"**{name}** is already in your wishlist.", ephemeral=True
            )
            return

        user_list[appid] = {
            "name":          name,
            "last_discount": discount,
            "added_at":      datetime.now(ZoneInfo("UTC")).isoformat(),
        }
        save_wishlists(wishlists)
        await interaction.response.send_message(
            f"Added **{name}** to your wishlist. You'll be DM'd when it goes on sale.", ephemeral=True
        )


# ── Cog ───────────────────────────────────────────────────────────────────────

class SteamCog(commands.Cog):

    steam = app_commands.Group(name="steam", description="Steam store commands")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Sale check task ───────────────────────────────────────────────────────

    @tasks.loop(minutes=30)
    async def check_sales(self):
        wishlists = load_wishlists()
        if not wishlists:
            return

        changed = False
        for uid, games in wishlists.items():
            for appid, info in list(games.items()):
                detail = await fetch_appdetails(int(appid))
                if not detail:
                    continue

                price_data   = detail.get("price_overview", {})
                discount     = price_data.get("discount_percent", 0) if price_data else 0
                last_discount = info.get("last_discount", 0)

                # Alert if newly on sale
                if discount > 0 and last_discount == 0:
                    final     = price_data.get("final_formatted", "?")
                    initial   = price_data.get("initial_formatted", "?")
                    store_url = f"https://store.steampowered.com/app/{appid}/"
                    embed = discord.Embed(
                        title=f"🏷️ {info['name']} is on sale!",
                        url=store_url,
                        color=0x57F287,
                        timestamp=datetime.now(ZoneInfo("UTC")),
                    )
                    embed.add_field(name="Sale",       value=f"~~{initial}~~ **{final}** (-{discount}%)", inline=False)
                    embed.add_field(name="Store Page", value=f"[View on Steam]({store_url})", inline=False)
                    embed.set_footer(text="Steam Wishlist Alert  •  cata.ai")

                    try:
                        user = await self.bot.fetch_user(int(uid))
                        await user.send(embed=embed)
                        log.info(f"Sale alert sent to {uid} for {info['name']}")
                    except Exception as e:
                        log.warning(f"Could not DM user {uid}: {e}")

                # Update stored discount
                if discount != last_discount:
                    info["last_discount"] = discount
                    changed = True

        if changed:
            save_wishlists(wishlists)

    @check_sales.before_loop
    async def before_check_sales(self):
        await self.bot.wait_until_ready()

    async def cog_load(self):
        self.check_sales.start()

    async def cog_unload(self):
        self.check_sales.cancel()

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Remove wishlist if user is no longer in any guild the bot serves."""
        uid = str(member.id)
        # Check if user is still in any other guild the bot is in
        still_present = any(
            member.id in [m.id for m in guild.members]
            for guild in self.bot.guilds
            if guild.id != member.guild.id
        )
        if still_present:
            return
        wishlists = load_wishlists()
        if uid in wishlists:
            del wishlists[uid]
            save_wishlists(wishlists)
            log.info(f"Removed Steam wishlist for departed user {uid}")

    # ── Commands ──────────────────────────────────────────────────────────────

    @steam.command(name="search", description="Search for a game on Steam")
    @app_commands.describe(query="Game name to search for")
    async def steam_search(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        results = await fetch_search(query)
        if not results:
            await interaction.followup.send(f"❌ No games found matching **{query}**.")
            return

        details_raw = await asyncio.gather(*[fetch_appdetails(r["id"]) for r in results])
        details     = [d for d in details_raw if d]

        if not details:
            await interaction.followup.send(f"❌ Could not retrieve game details for **{query}**.")
            return

        embed   = build_game_embed(details[0], 1, len(details))
        content = f"Found **{len(details)}** game(s) matching **\"{query}\"**:"
        await interaction.followup.send(content=content, embed=embed, view=SteamPaginator(details))

    @steam.command(name="wishlist", description="View your Steam wishlist and sale alerts")
    async def steam_wishlist(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        uid       = str(interaction.user.id)
        wishlists = load_wishlists()
        games     = wishlists.get(uid, {})

        if not games:
            await interaction.followup.send("Your Steam wishlist is empty. Use the **Wishlist** button on `/steam search` to add games.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"{interaction.user.display_name}'s Steam Wishlist",
            color=0x1b2838,
            timestamp=datetime.now(ZoneInfo("UTC")),
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        for appid, info in games.items():
            store_url = f"https://store.steampowered.com/app/{appid}/"
            discount  = info.get("last_discount", 0)
            status    = f"🏷️ **{discount}% OFF** right now!" if discount > 0 else "Not on sale"
            embed.add_field(
                name=info["name"],
                value=f"{status}\n[View on Steam]({store_url})",
                inline=False,
            )

        embed.set_footer(text=f"Steam Wishlist  •  {len(games)} game(s)  •  cata.ai")
        await interaction.followup.send(embed=embed, ephemeral=True, view=WishlistManageView(uid))

    @steam.command(name="unwishlist", description="Remove a game from your Steam wishlist")
    @app_commands.describe(name="Name of the game to remove")
    async def steam_unwishlist(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)
        uid       = str(interaction.user.id)
        wishlists = load_wishlists()
        games     = wishlists.get(uid, {})

        match = next((aid for aid, info in games.items() if info["name"].lower() == name.lower()), None)
        if not match:
            await interaction.followup.send(f"❌ **{name}** not found in your wishlist.", ephemeral=True)
            return

        removed_name = games[match]["name"]
        del games[match]
        save_wishlists(wishlists)
        await interaction.followup.send(f"Removed **{removed_name}** from your wishlist.", ephemeral=True)


# ── Wishlist manage view (remove buttons) ─────────────────────────────────────

class WishlistManageView(discord.ui.View):
    def __init__(self, uid: str):
        super().__init__(timeout=120)
        self.uid = uid
        wishlists = load_wishlists()
        games     = wishlists.get(uid, {})
        for appid, info in list(games.items())[:5]:  # max 5 buttons
            self.add_item(WishlistRemoveButton(appid, info["name"]))


class WishlistRemoveButton(discord.ui.Button):
    def __init__(self, appid: str, name: str):
        super().__init__(label=f"Remove {name[:40]}", style=discord.ButtonStyle.danger)
        self.appid = appid

    async def callback(self, interaction: discord.Interaction):
        uid       = str(interaction.user.id)
        wishlists = load_wishlists()
        games     = wishlists.get(uid, {})

        if self.appid not in games:
            await interaction.response.send_message("Already removed.", ephemeral=True)
            return

        name = games[self.appid]["name"]
        del games[self.appid]
        save_wishlists(wishlists)
        self.disabled = True
        self.label    = f"Removed {name[:40]}"
        await interaction.response.edit_message(view=self.view)
        await interaction.followup.send(f"Removed **{name}** from your wishlist.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SteamCog(bot))
