import discord
from discord import app_commands
from discord.ext import commands
import requests
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_RESULTS = 5


# ── Data helpers ──────────────────────────────────────────────────────────────

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

    # Price
    price_data       = detail.get("price_overview")
    if price_data:
        final        = price_data.get("final_formatted", "N/A")
        discount     = price_data.get("discount_percent", 0)
        initial      = price_data.get("initial_formatted", "")
        if discount > 0:
            price_str = f"~~{initial}~~ **{final}** 🏷️ -{discount}% OFF"
        else:
            price_str = final
    elif detail.get("is_free"):
        price_str = "**Free to Play**"
    else:
        price_str = "N/A"

    # Rating
    metacritic = detail.get("metacritic", {})
    meta_score = metacritic.get("score") if metacritic else None
    reviews    = detail.get("review_score_desc") or detail.get("recommendations")

    # Release date
    release    = detail.get("release_date", {})
    rel_date   = release.get("date", "Unknown") if release else "Unknown"

    # Platforms
    platforms  = detail.get("platforms", {})
    plat_icons = " ".join(filter(None, [
        "Windows" if platforms.get("windows") else "",
        "Mac"     if platforms.get("mac")     else "",
        "Linux"   if platforms.get("linux")   else "",
    ]))

    embed = discord.Embed(
        title=name,
        url=store_url,
        description=description,
        color=0x1b2838,
        timestamp=datetime.now(ZoneInfo("UTC")),
    )
    if header:
        embed.set_thumbnail(url=header)

    embed.add_field(name="💵 Price",     value=price_str or "N/A",    inline=True)
    embed.add_field(name="📅 Released",  value=rel_date,               inline=True)
    embed.add_field(name="💻 Platforms", value=plat_icons or "Unknown", inline=True)

    if meta_score:
        embed.add_field(name="🎯 Metacritic", value=f"{meta_score}/100", inline=True)

    embed.add_field(name="🔗 Store Page", value=f"[View on Steam]({store_url})", inline=False)
    embed.set_footer(text=f"Steam  •  Result {page} of {total}")
    return embed


# ── Paginator ─────────────────────────────────────────────────────────────────

class SteamPaginator(discord.ui.View):
    def __init__(self, details: list[dict]):
        super().__init__(timeout=120)
        self.details = details
        self.page    = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page == len(self.details) - 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(
            embed=build_game_embed(self.details[self.page], self.page + 1, len(self.details)),
            view=self,
        )

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(
            embed=build_game_embed(self.details[self.page], self.page + 1, len(self.details)),
            view=self,
        )


# ── Cog ───────────────────────────────────────────────────────────────────────

class SteamCog(commands.Cog):

    steam = app_commands.Group(name="steam", description="Steam store commands")

    @steam.command(name="search", description="Search for a game on Steam")
    @app_commands.describe(query="Game name to search for")
    async def steam_search(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        results = await fetch_search(query)
        if not results:
            await interaction.followup.send(f"❌ No games found matching **{query}**.")
            return

        # Fetch full details for each result concurrently
        details_raw = await asyncio.gather(*[fetch_appdetails(r["id"]) for r in results])
        details     = [d for d in details_raw if d]

        if not details:
            await interaction.followup.send(f"❌ Could not retrieve game details for **{query}**.")
            return

        embed = build_game_embed(details[0], 1, len(details))
        content = f"Found **{len(details)}** game(s) matching **\"{query}\"**:"

        if len(details) > 1:
            await interaction.followup.send(content=content, embed=embed, view=SteamPaginator(details))
        else:
            await interaction.followup.send(content=content, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(SteamCog(bot))
