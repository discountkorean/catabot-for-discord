from catabot.shopify import (
    _next_link,
    base_url,
    build_variant_map,
    display_domain,
    normalize_product_js,
    product_url,
)


class TestUrlHelpers:
    def test_base_url_strips_path_and_query(self):
        assert base_url("https://www.store.com/products.json?limit=250") == "https://www.store.com"

    def test_display_domain_rewrites_secure(self):
        assert display_domain("secure.store.com") == "www.store.com"
        assert display_domain("www.store.com") == "www.store.com"

    def test_product_url_uses_display_domain(self):
        assert product_url("https://secure.store.com/x", "my-handle") == "https://www.store.com/products/my-handle"


class TestNextLink:
    def test_extracts_next(self):
        header = '<https://store.com/products.json?page_info=abc>; rel="next"'
        assert _next_link(header) == "https://store.com/products.json?page_info=abc"

    def test_prefers_next_over_previous(self):
        header = (
            '<https://store.com/p?page_info=prev>; rel="previous", <https://store.com/p?page_info=next>; rel="next"'
        )
        assert _next_link(header) == "https://store.com/p?page_info=next"

    def test_no_next_returns_none(self):
        assert _next_link('<https://store.com/p?page_info=prev>; rel="previous"') is None
        assert _next_link("") is None


class TestBuildVariantMap:
    def test_flattens_by_variant_id(self):
        products = [
            {
                "handle": "tee",
                "title": "Tee",
                "images": [{"src": "https://cdn/tee.jpg"}],
                "variants": [
                    {"id": 1, "title": "S", "price": "10.00", "available": True},
                    {"id": 2, "title": "M", "price": "10.00", "available": False},
                ],
            }
        ]
        vmap = build_variant_map(products)
        assert set(vmap) == {"1", "2"}
        assert vmap["1"] == {
            "available": True,
            "title": "Tee",
            "variant_title": "S",
            "price": "10.00",
            "handle": "tee",
            "image_url": "https://cdn/tee.jpg",
        }
        assert vmap["2"]["available"] is False

    def test_handles_missing_images(self):
        vmap = build_variant_map([{"handle": "x", "title": "X", "variants": [{"id": 9, "title": "OS"}]}])
        assert vmap["9"]["image_url"] is None


class TestNormalizeProductJs:
    def test_converts_cent_price_ints(self):
        p = {"variants": [{"id": 1, "title": "S", "price": 4000}], "images": []}
        out = normalize_product_js(p)
        assert out["variants"][0]["price"] == "40.00"

    def test_rewrites_string_images_to_dicts(self):
        p = {"variants": [], "images": ["//cdn/a.jpg", "https://cdn/b.jpg"]}
        out = normalize_product_js(p)
        assert out["images"][0] == {"src": "https://cdn/a.jpg"}
        assert out["images"][1] == {"src": "https://cdn/b.jpg"}
