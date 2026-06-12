"""Discord embed builders for every alert and listing type.

Pure rendering: each function takes plain dicts and returns a
:class:`discord.Embed`. No network or state access beyond the cached footer.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import discord

from .shopify import display_domain, product_url
from .storage import bot_footer

# Default number of changed products that triggers a single mass-drop summary
# instead of one embed per product. Overridable per guild.
AGGREGATE_THRESHOLD = 20


def _utcnow() -> datetime:
    return datetime.now(ZoneInfo("UTC"))


def _format_sizes(variant_titles: list[str]) -> tuple[str, str]:
    """Return ``(field_name, field_value)``, hiding ``Default Title`` placeholders."""
    filtered = [t for t in variant_titles if t.lower() != "default title"]
    if not filtered:
        return "Variants", "N/A"
    return "Variants", ", ".join(filtered)


def _size_list(variants: list, available_only: bool = False) -> str:
    sizes = [
        v["variant_title"]
        for v in variants
        if (not available_only or v.get("available", True)) and v["variant_title"].lower() != "default title"
    ]
    return ", ".join(sizes) if sizes else "—"


def make_aggregate_embed(store_name: str, store_url: str, restocked: dict, new_items: dict) -> discord.Embed:
    """Summarise many simultaneous changes as one 'mass drop' embed."""
    domain = display_domain(store_url.split("/")[2])
    total = len(restocked) + len(new_items)
    footer = f"{bot_footer()} • {domain}"
    embed_limit = 5800

    lines = []
    for variants in restocked.values():
        lines.append(f"🟢 **{variants[0]['title']}** ({_size_list(variants)})")
    for variants in new_items.values():
        sizes = _size_list([v for v in variants if v.get("available")]) or _size_list(variants)
        lines.append(f"🟠 **{variants[0]['title']}** ({sizes})")

    title_text = f"📦 Mass Drop: {store_name} — {total} items"
    used = len(title_text) + len(footer)
    shown = []
    for line in lines:
        if used + len(line) + 1 > embed_limit:
            embed = discord.Embed(
                title=f"📦 Mass Drop: {store_name}",
                description=f"**{total}** item{'s' if total != 1 else ''} updated — check the store for details.",
                color=0x5865F2,
                timestamp=_utcnow(),
            )
            embed.set_footer(text=footer)
            return embed
        shown.append(line)
        used += len(line) + 1

    embed = discord.Embed(title=title_text, color=0x5865F2, timestamp=_utcnow())
    embed.set_footer(text=footer)

    chunk: list[str] = []
    chunks: list[list[str]] = []
    for line in shown:
        if sum(len(item) + 1 for item in chunk) + len(line) > 1000:
            chunks.append(chunk)
            chunk = []
        chunk.append(line)
    if chunk:
        chunks.append(chunk)
    for i, ch in enumerate(chunks):
        embed.add_field(name="Items" if i == 0 else "​", value="\n".join(ch), inline=False)

    return embed


def make_restock_embed(store_name: str, store_url: str, variants: list) -> discord.Embed:
    first = variants[0]
    size_name, sizes = _format_sizes([v["variant_title"] for v in variants])
    domain = display_domain(store_url.split("/")[2])
    embed = discord.Embed(title=f"🔔 Back in Stock: {first['title']}", color=0x57F287, timestamp=_utcnow())
    if first.get("image_url"):
        embed.set_thumbnail(url=first["image_url"])
    embed.add_field(name=size_name, value=sizes, inline=True)
    embed.add_field(name="Price", value=f"${float(first['price']):.2f}", inline=True)
    embed.add_field(name="Store", value=store_name, inline=True)
    embed.add_field(name="Stock", value="✅ In Stock", inline=True)
    embed.add_field(name="View Product", value=product_url(store_url, first["handle"]), inline=False)
    embed.set_footer(text=f"{bot_footer()} • {domain}")
    return embed


def make_new_item_embed(store_name: str, store_url: str, variants: list) -> discord.Embed:
    first = variants[0]
    domain = display_domain(store_url.split("/")[2])
    in_stock = [v["variant_title"] for v in variants if v["available"] and v["variant_title"].lower() != "default title"]
    out_stock = [
        v["variant_title"] for v in variants if not v["available"] and v["variant_title"].lower() != "default title"
    ]
    size_lines = ""
    if in_stock:
        size_lines += "✅ " + ", ".join(in_stock)
    if out_stock:
        size_lines += ("\n" if size_lines else "") + "❌ " + ", ".join(out_stock)
    embed = discord.Embed(title=f"🆕 New Item: {first['title']}", color=0xFEE75C, timestamp=_utcnow())
    if first.get("image_url"):
        embed.set_thumbnail(url=first["image_url"])
    embed.add_field(name="Variants", value=size_lines or "N/A", inline=True)
    embed.add_field(name="Price", value=f"${float(first['price']):.2f}", inline=True)
    embed.add_field(name="Store", value=store_name, inline=True)
    embed.add_field(name="View Product", value=product_url(store_url, first["handle"]), inline=False)
    embed.set_footer(text=f"{bot_footer()} • {domain}")
    return embed


def make_removed_embed(store_name: str, store_url: str, variants: list) -> discord.Embed:
    first = variants[0]
    size_name, sizes = _format_sizes([v["variant_title"] for v in variants])
    domain = display_domain(store_url.split("/")[2])
    embed = discord.Embed(title=f"🗑️ Item Removed: {first['title']}", color=0x95A5A6, timestamp=_utcnow())
    if first.get("image_url"):
        embed.set_thumbnail(url=first["image_url"])
    embed.add_field(name=f"Last Known {size_name}", value=sizes, inline=True)
    embed.add_field(name="Store", value=store_name, inline=True)
    embed.set_footer(text=f"{bot_footer()} • {domain}")
    return embed


def make_sold_out_embed(store_name: str, store_url: str, variants: list) -> discord.Embed:
    first = variants[0]
    domain = display_domain(store_url.split("/")[2])
    _, sizes = _format_sizes([v["variant_title"] for v in variants])
    embed = discord.Embed(title=f"🔴 Sold Out: {first['title']}", color=0xED4245, timestamp=_utcnow())
    if first.get("image_url"):
        embed.set_thumbnail(url=first["image_url"])
    embed.add_field(name="Sizes", value=sizes, inline=True)
    embed.add_field(name="Store", value=store_name, inline=True)
    embed.add_field(name="View Product", value=product_url(store_url, first["handle"]), inline=False)
    embed.set_footer(text=f"{bot_footer()} • {domain}")
    return embed


def make_price_change_embed(store_name: str, store_url: str, variants: list) -> discord.Embed:
    """``variants`` carry an extra ``old_price`` key alongside the new ``price``."""
    first = variants[0]
    domain = display_domain(store_url.split("/")[2])
    embed = discord.Embed(title=f"💲 Price Change: {first['title']}", color=0x5865F2, timestamp=_utcnow())
    if first.get("image_url"):
        embed.set_thumbnail(url=first["image_url"])
    lines = []
    for v in variants:
        old_p = f"${float(v['old_price']):.2f}"
        new_p = f"${float(v['price']):.2f}"
        label = v["variant_title"]
        if label.lower() == "default title":
            lines.append(f"{old_p} → **{new_p}**")
        else:
            lines.append(f"**{label}**: {old_p} → **{new_p}**")
    embed.add_field(name="Price", value="\n".join(lines), inline=False)
    embed.add_field(name="Store", value=store_name, inline=True)
    embed.add_field(name="View Product", value=product_url(store_url, first["handle"]), inline=False)
    embed.set_footer(text=f"{bot_footer()} • {domain}")
    return embed
