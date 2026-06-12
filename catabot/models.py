"""Domain data structures and pure matching/normalization logic.

Everything here is side-effect free and independent of discord, which makes it
the natural home for the bot's unit-tested core (size normalization,
subscription matching, legacy migration).
"""

from __future__ import annotations

import uuid
from typing import Any

from .shopify import display_domain

# Canonical size tokens. Keys are pre-normalized (lowercased, stripped of
# spaces/dashes/underscores); values are the canonical form used for matching.
_SIZE_ALIASES: dict[str, str] = {
    "xs": "xs", "xsmall": "xs", "xsm": "xs", "extrasmall": "xs",
    "s": "s", "small": "s", "sm": "s",
    "m": "m", "med": "m", "medium": "m",
    "l": "l", "large": "l", "lg": "l",
    "xl": "xl", "xlarge": "xl", "extralarge": "xl",
    "2xl": "2xl", "xxl": "2xl", "xxlarge": "2xl", "2xlarge": "2xl", "doublexl": "2xl",
    "3xl": "3xl", "xxxl": "3xl", "3xlarge": "3xl", "triplexl": "3xl",
    "4xl": "4xl", "xxxxl": "4xl",
    "5xl": "5xl",
}


def normalize_size(token: str) -> str:
    """Normalize a size token to its canonical form for comparison."""
    cleaned = token.lower().strip().replace(" ", "").replace("-", "").replace("_", "")
    return _SIZE_ALIASES.get(cleaned, cleaned)


def variant_size_tokens(variant_title: str) -> list[str]:
    """Extract canonical size tokens from a title like ``Black / Small``."""
    return [normalize_size(seg.strip()) for seg in variant_title.replace(",", "/").split("/")]


def sub_matches(sub: dict, store_name: str, variant: dict) -> bool:
    """Return True if a user/role subscription's filters match this variant.

    ``stores``/``names`` are AND-style narrowing filters; ``sizes`` matches if
    ANY requested size is present in the variant.
    """
    if sub.get("stores") and store_name not in sub["stores"]:
        return False
    if sub.get("names"):
        search_text = (variant["title"] + " " + variant["variant_title"]).lower()
        if not all(kw.lower() in search_text for kw in sub["names"]):
            return False
    if sub.get("sizes"):
        vtokens = variant_size_tokens(variant["variant_title"])
        if not any(s in vtokens for s in sub["sizes"]):
            return False
    return True


def migrate_notifications(gs: dict) -> bool:
    """Convert a legacy ``notifications`` dict into ``subscriptions`` in place.

    Returns True if a migration occurred (and the legacy key was removed).
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


class SearchResult:
    """A single product hit, prepared for rendering in a search embed."""

    def __init__(self, store_name: str, store_url: str, product: dict):
        self.store_name = store_name
        raw_base = "/".join(store_url.split("/")[:3])
        scheme, _, dom = raw_base.partition("://")
        self.store_base = f"{scheme}://{display_domain(dom)}"
        self.title = product.get("title", "Unknown")
        self.handle = product.get("handle", "")
        self.image_url = (product.get("images") or [{}])[0].get("src")
        self.product_url = f"{self.store_base}/products/{self.handle}"

        self.available: list[dict[str, Any]] = []
        self.unavailable: list[dict[str, Any]] = []
        for v in product.get("variants", []):
            entry = {
                "size": v.get("title", ""),
                "price": v.get("price", "0.00"),
                "variant_id": v["id"],
                "cart_url": f"{self.store_base}/cart/{v['id']}:1",
            }
            (self.available if v.get("available") else self.unavailable).append(entry)

    @property
    def price(self) -> str:
        src = self.available or self.unavailable
        return f"${float(src[0]['price']):.2f}" if src else "N/A"
