import discord

from catabot.embeds import (
    _format_sizes,
    _size_list,
    make_aggregate_embed,
    make_restock_embed,
)


class TestFormatSizes:
    def test_hides_default_title(self):
        assert _format_sizes(["Default Title"]) == ("Variants", "N/A")

    def test_joins_real_variants(self):
        name, value = _format_sizes(["S", "M", "L"])
        assert name == "Variants"
        assert value == "S, M, L"


class TestSizeList:
    def test_filters_default_title(self):
        assert _size_list([{"variant_title": "Default Title", "available": True}]) == "—"

    def test_available_only(self):
        variants = [
            {"variant_title": "S", "available": True},
            {"variant_title": "M", "available": False},
        ]
        assert _size_list(variants, available_only=True) == "S"
        assert _size_list(variants, available_only=False) == "S, M"


class TestRestockEmbed:
    def test_builds_embed_with_title_and_price(self):
        variants = [
            {
                "title": "Logo Tee",
                "variant_title": "S",
                "price": "40.00",
                "handle": "logo-tee",
                "image_url": None,
                "available": True,
            }
        ]
        embed = make_restock_embed("StoreA", "https://www.store.com/x", variants)
        assert isinstance(embed, discord.Embed)
        assert "Back in Stock: Logo Tee" in embed.title
        prices = [f.value for f in embed.fields if f.name == "Price"]
        assert prices == ["$40.00"]


class TestAggregateEmbed:
    def test_counts_items_in_title(self):
        restocked = {"a": [{"title": "A", "variant_title": "S", "available": True}]}
        new_items = {"b": [{"title": "B", "variant_title": "M", "available": True}]}
        embed = make_aggregate_embed("StoreA", "https://www.store.com/x", restocked, new_items)
        assert "2 items" in embed.title
