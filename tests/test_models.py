from catabot.models import (
    SearchResult,
    migrate_notifications,
    normalize_size,
    sub_matches,
    variant_size_tokens,
)


class TestNormalizeSize:
    def test_canonical_aliases(self):
        assert normalize_size("Small") == "s"
        assert normalize_size("SM") == "s"
        assert normalize_size("x-large") == "xl"
        assert normalize_size("XXL") == "2xl"
        assert normalize_size("extra small") == "xs"

    def test_unknown_passthrough(self):
        assert normalize_size("OneSize") == "onesize"
        assert normalize_size("42") == "42"

    def test_whitespace_and_case(self):
        assert normalize_size("  MEDIUM  ") == "m"


class TestVariantSizeTokens:
    def test_splits_on_slash_and_comma(self):
        assert variant_size_tokens("Black / Small") == ["black", "s"]
        assert variant_size_tokens("Red, Large") == ["red", "l"]

    def test_single_token(self):
        assert variant_size_tokens("Medium") == ["m"]


class TestSubMatches:
    def _variant(self, title="Logo Hoodie", variant_title="Black / Small"):
        return {"title": title, "variant_title": variant_title}

    def test_empty_filters_match_everything(self):
        assert sub_matches({}, "AnyStore", self._variant()) is True

    def test_store_filter(self):
        sub = {"stores": ["StoreA"]}
        assert sub_matches(sub, "StoreA", self._variant()) is True
        assert sub_matches(sub, "StoreB", self._variant()) is False

    def test_names_are_and_logic(self):
        sub = {"names": ["logo", "hoodie"]}
        assert sub_matches(sub, "S", self._variant(title="Logo Hoodie")) is True
        sub2 = {"names": ["logo", "jacket"]}
        assert sub_matches(sub2, "S", self._variant(title="Logo Hoodie")) is False

    def test_sizes_are_any_logic(self):
        sub = {"sizes": ["s"]}
        assert sub_matches(sub, "S", self._variant(variant_title="Black / Small")) is True
        assert sub_matches(sub, "S", self._variant(variant_title="Black / Large")) is False


class TestMigrateNotifications:
    def test_no_notifications_is_noop(self):
        gs = {"subscriptions": []}
        assert migrate_notifications(gs) is False

    def test_converts_users_and_roles(self):
        gs = {"notifications": {"StoreA": {"users": [1, 2], "roles": [9]}}}
        assert migrate_notifications(gs) is True
        assert "notifications" not in gs
        types = sorted((s["type"], s["target_id"]) for s in gs["subscriptions"])
        assert types == [("role", 9), ("user", 1), ("user", 2)]

    def test_legacy_list_form(self):
        gs = {"notifications": {"StoreA": [5]}}
        assert migrate_notifications(gs) is True
        assert gs["subscriptions"][0] == {
            "id": gs["subscriptions"][0]["id"],
            "type": "user",
            "target_id": 5,
            "stores": ["StoreA"],
            "names": [],
            "sizes": [],
        }

    def test_idempotent_dedup(self):
        gs = {
            "subscriptions": [{"type": "user", "target_id": 1, "stores": [], "names": [], "sizes": []}],
            "notifications": {"StoreA": {"users": [1], "roles": []}},
        }
        migrate_notifications(gs)
        users = [s for s in gs["subscriptions"] if s["target_id"] == 1]
        assert len(users) == 1


class TestSearchResult:
    def test_parses_variants_and_price(self):
        product = {
            "title": "Logo Tee",
            "handle": "logo-tee",
            "images": [{"src": "https://cdn/img.jpg"}],
            "variants": [
                {"id": 111, "title": "S", "price": "40.00", "available": True},
                {"id": 222, "title": "M", "price": "40.00", "available": False},
            ],
        }
        r = SearchResult("StoreA", "https://secure.store.com/products.json", product)
        assert r.store_base == "https://www.store.com"
        assert r.product_url == "https://www.store.com/products/logo-tee"
        assert len(r.available) == 1
        assert len(r.unavailable) == 1
        assert r.available[0]["cart_url"] == "https://www.store.com/cart/111:1"
        assert r.price == "$40.00"

    def test_price_na_when_no_variants(self):
        r = SearchResult("StoreA", "https://store.com/products.json", {"variants": []})
        assert r.price == "N/A"
