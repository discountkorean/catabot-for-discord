"""Shopify storefront client and URL helpers.

All network access goes through a single shared :data:`SESSION`. It uses
``curl_cffi`` with ``impersonate="chrome"`` so the TLS/JA3 + HTTP2 fingerprint
matches a real browser — plain ``requests``/``urllib3`` has a distinct TLS
signature that Cloudflare-fronted Shopify stores answer with ``429`` regardless
of spoofed headers. curl_cffi also supplies the matching browser headers, so we
do not set any ourselves.

Synchronous fetch functions (``*_sync``) are intended to be wrapped in
:func:`asyncio.to_thread` by callers so they never block the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from curl_cffi import requests
from curl_cffi.requests import exceptions as requests_exceptions

log = logging.getLogger(__name__)

SESSION = requests.Session(impersonate="chrome")

# Shopify paginates products.json at most 250 per page.
PAGE_LIMIT = 250


def base_url(url: str) -> str:
    """Strip path/query from a store URL down to ``https://domain``."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def display_domain(domain: str) -> str:
    """Normalize a ``secure.`` checkout domain back to its public ``www.`` form."""
    if domain.startswith("secure."):
        return "www." + domain[len("secure.") :]
    return domain


def product_url(store_url: str, handle: str) -> str:
    """Build the public product page URL for a handle."""
    p = urlparse(store_url)
    domain = display_domain(p.netloc)
    return f"{p.scheme}://{domain}/products/{handle}"


def _next_link(link_header: str) -> str | None:
    """Extract the ``rel="next"`` URL from a Shopify ``Link`` header, if present."""
    for part in link_header.split(","):
        if 'rel="next"' in part:
            m = re.search(r"<([^>]+)>", part)
            return m.group(1) if m else None
    return None


def fetch_paginated_sync(url: str, key: str, delay: float = 0.5) -> tuple[list, bool]:
    """Fetch every page of a Shopify endpoint. Returns ``(items, password_locked)``.

    Prefers cursor-based pagination (``Link`` header). If the first response has
    no ``Link`` header but returns a full page, falls back to legacy page-based
    (``?page=N``) pagination — required for stores like Gymshark.
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["limit"] = [str(PAGE_LIMIT)]
    qs.pop("page", None)
    qs.pop("page_info", None)
    first_url = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))

    results: list = []
    password_locked = False

    # ── Page 1 ──────────────────────────────────────────────────────────────
    try:
        r = SESSION.get(first_url, timeout=15)
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
        link_header = r.headers.get("Link", "")
    except Exception as e:  # noqa: BLE001
        log.error(f"Failed to fetch page 1 of {url}: {e}")
        return [], False

    next_url = _next_link(link_header)
    use_page_based = not next_url and len(batch) == PAGE_LIMIT
    if use_page_based:
        log.debug(f"No Link header on first page — switching to page-based pagination for {url}")

    # ── Cursor-based: follow Link headers ───────────────────────────────────
    if not use_page_based:
        page_num = 1
        while next_url:
            page_num += 1
            time.sleep(delay)
            try:
                r = SESSION.get(next_url, timeout=15)
                if r.status_code == 429:
                    log.warning(f"Rate limited on page {page_num}, retrying in 5s")
                    time.sleep(5)
                    r = SESSION.get(next_url, timeout=15)
                if r.status_code in (401, 403) or "password" in r.url:
                    password_locked = True
                    break
                r.raise_for_status()
                data = r.json()
                link_header = r.headers.get("Link", "")
                if not isinstance(data, dict):
                    break
                results.extend(data.get(key, []))
                next_url = _next_link(link_header)
            except requests_exceptions.HTTPError:
                break
            except Exception as e:  # noqa: BLE001
                log.error(f"Failed to fetch page {page_num}: {e}")
                break
        log.debug(f"Cursor pagination done: {page_num} page(s), {len(results)} total {key}")

    # ── Page-based: increment ?page= until empty ────────────────────────────
    else:
        page_num = 1
        qs.pop("page_info", None)
        while True:
            page_num += 1
            qs["page"] = [str(page_num)]
            page_url = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
            time.sleep(delay)
            try:
                r = SESSION.get(page_url, timeout=15)
                if r.status_code == 429:
                    log.warning(f"Rate limited on page {page_num}, retrying in 5s")
                    time.sleep(5)
                    r = SESSION.get(page_url, timeout=15)
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
            except requests_exceptions.HTTPError:
                break
            except Exception as e:  # noqa: BLE001
                log.error(f"Failed to fetch page {page_num}: {e}")
                break
        log.debug(f"Page-based pagination done: {page_num - 1} page(s), {len(results)} total {key}")

    return results, password_locked


def normalize_product_js(p: dict) -> dict:
    """Coerce a ``/products/{handle}.js`` payload into ``products.json`` shape."""
    for v in p.get("variants", []):
        if isinstance(v.get("price"), int):
            v["price"] = f"{v['price'] / 100:.2f}"
    raw_images = p.get("images", [])
    if raw_images and isinstance(raw_images[0], str):
        p["images"] = [{"src": ("https:" + img if img.startswith("//") else img)} for img in raw_images]
    return p


def fetch_watched_handles_sync(base: str, handles: list[str]) -> list:
    """Fetch specific product handles via ``/products/{handle}.js``.

    A 404 yields a ``{"handle": ..., "_removed": True}`` marker so callers can
    detect deletions.
    """
    products: list = []
    for handle in handles:
        try:
            r = SESSION.get(f"{base}/products/{handle}.js", timeout=10)
            if r.status_code == 404:
                products.append({"handle": handle, "_removed": True})
                continue
            if not r.ok:
                continue
            products.append(normalize_product_js(r.json()))
        except Exception as e:  # noqa: BLE001
            log.error(f"Failed to fetch watched handle {handle}: {e}")
    return products


def search_suggest_sync(base: str, query: str, limit: int = 10) -> list:
    """Resolve a search query to full product dicts via ``/search/suggest.json``."""
    try:
        r = SESSION.get(
            f"{base}/search/suggest.json",
            params={
                "q": query,
                "resources[type]": "product",
                "resources[limit]": limit,
                "resources[options][unavailable_products]": "show",
                "resources[options][fields]": "title,variants.title,vendor",
            },
            timeout=10,
        )
        if not r.ok:
            return []
        handles = [
            p["handle"]
            for p in r.json().get("resources", {}).get("results", {}).get("products", [])
            if p.get("handle")
        ]
    except Exception as e:  # noqa: BLE001
        log.error(f"suggest failed for {base}: {e}")
        return []

    products: list = []
    for handle in handles:
        try:
            rp = SESSION.get(f"{base}/products/{handle}.js", timeout=10)
            if not rp.ok:
                continue
            products.append(normalize_product_js(rp.json()))
        except Exception as e:  # noqa: BLE001
            log.error(f"product .js fetch failed for {base}/products/{handle}: {e}")
    return products


def probe_shopify_sync(url: str) -> bool:
    """Return True if ``url`` is a reachable, unlocked Shopify ``products.json``."""
    for attempt in range(2):
        try:
            r = SESSION.get(url, timeout=20)
            if r.status_code in (401, 403) or "password" in r.url:
                return False
            if not r.ok:
                return False
            data = r.json()
            return isinstance(data, dict) and "products" in data
        except requests_exceptions.Timeout:
            if attempt == 0:
                continue
            return False
        except Exception:  # noqa: BLE001
            return False
    return False


def build_variant_map(products: list) -> dict[str, dict[str, Any]]:
    """Flatten a product list into ``{variant_id: variant_info}`` for diffing."""
    variants: dict[str, dict[str, Any]] = {}
    for product in products:
        handle = product.get("handle", "")
        title = product.get("title", "Unknown")
        images = product.get("images", [])
        image_url = images[0]["src"] if images else None
        for v in product.get("variants", []):
            variants[str(v["id"])] = {
                "available": v.get("available", False),
                "title": title,
                "variant_title": v.get("title", ""),
                "price": v.get("price", "0.00"),
                "handle": handle,
                "image_url": image_url,
            }
    return variants


async def search_suggest(base: str, query: str) -> list:
    return await asyncio.to_thread(search_suggest_sync, base, query)


async def fetch_products(url: str) -> tuple[list, bool]:
    """Fetch all products for a store. Returns ``(products, password_locked)``."""
    base = base_url(url)
    return await asyncio.to_thread(fetch_paginated_sync, f"{base}/products.json", "products", 0.5)


async def discover_shopify_url(raw: str) -> str | None:
    """Find the working ``products.json`` endpoint for a human-entered URL.

    Tries the bare domain, then ``www.`` / ``secure.`` variants in priority
    order. Returns the first reachable endpoint, or None.
    """
    if not raw.startswith("http"):
        raw = "https://" + raw

    domain = urlparse(raw).netloc or raw.split("/")[2]
    candidates = [f"https://{domain}/products.json?limit={PAGE_LIMIT}"]
    if domain.startswith("www."):
        bare = domain[4:]
        candidates.append(f"https://secure.{bare}/products.json?limit={PAGE_LIMIT}")
    elif domain.startswith("secure."):
        bare = domain[7:]
        candidates.append(f"https://www.{bare}/products.json?limit={PAGE_LIMIT}")
    else:
        candidates.append(f"https://www.{domain}/products.json?limit={PAGE_LIMIT}")
        candidates.append(f"https://secure.{domain}/products.json?limit={PAGE_LIMIT}")

    for url in candidates:
        if await asyncio.to_thread(probe_shopify_sync, url):
            return url
    return None
