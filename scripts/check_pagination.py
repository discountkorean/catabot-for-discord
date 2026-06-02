"""
check_pagination.py — test Shopify pagination for a store URL.

Usage:
    python scripts/check_pagination.py <store_url>

Example:
    python scripts/check_pagination.py https://gymshark.com
"""

import re
import sys
import time
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests

HEADERS = {
    "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma":        "no-cache",
    "Expires":       "0",
}


def base_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def fetch_cursor(session: requests.Session, base: str) -> int:
    """Cursor-based pagination via Link header. Returns total products fetched."""
    print("\n── Cursor-based pagination (Link header) ──────────────────")
    endpoint = f"{base}/products.json"
    parsed   = urlparse(endpoint)
    qs       = parse_qs(parsed.query, keep_blank_values=True)
    qs["limit"] = ["250"]
    qs.pop("page", None)
    qs.pop("page_info", None)
    next_url = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))

    total = 0
    page  = 0

    while next_url:
        page += 1
        print(f"\n  [Page {page}] GET {next_url}")
        try:
            r = session.get(next_url, timeout=15)
        except Exception as e:
            print(f"    ERROR: {e}")
            break

        print(f"    Status : {r.status_code}")
        link   = r.headers.get("Link", "")
        print(f"    Link   : {link!r}")

        if r.status_code == 429:
            print("    Rate limited — waiting 5s")
            time.sleep(5)
            continue
        if r.status_code in (401, 403) or "password" in r.url:
            print("    Password protected — stopping.")
            break
        if not r.ok:
            print(f"    HTTP {r.status_code} — stopping.")
            break

        try:
            batch = r.json().get("products", [])
        except Exception as e:
            print(f"    JSON error: {e}")
            break

        total += len(batch)
        print(f"    Products : {len(batch)}  (running total: {total})")

        next_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                m = re.search(r"<([^>]+)>", part)
                if m:
                    next_url = m.group(1)
                    print(f"    Next cursor: {next_url}")
                break

        if not next_url:
            print("    No next cursor — done.")
        else:
            time.sleep(0.5)

    print(f"\n  CURSOR RESULT: {page} page(s), {total} products")
    return total


def fetch_page_based(session: requests.Session, base: str) -> int:
    """Legacy page-based pagination (?page=N). Returns total products fetched."""
    print("\n── Page-based pagination (?page=N) ────────────────────────")
    total      = 0
    page       = 0
    last_count = -1

    while True:
        page += 1
        url = f"{base}/products.json?limit=250&page={page}"
        print(f"\n  [Page {page}] GET {url}")
        try:
            r = session.get(url, timeout=15)
        except Exception as e:
            print(f"    ERROR: {e}")
            break

        print(f"    Status      : {r.status_code}")
        print(f"    Content-Type: {r.headers.get('Content-Type', '?')}")

        if r.status_code == 429:
            print("    Rate limited — waiting 5s")
            time.sleep(5)
            continue
        if r.status_code in (401, 403) or "password" in r.url:
            print("    Password protected — stopping.")
            break
        if not r.ok:
            print(f"    HTTP {r.status_code} — stopping.")
            break

        raw = r.text
        try:
            data  = r.json()
            batch = data.get("products", [])
        except Exception as e:
            print(f"    JSON error: {e}")
            print(f"    Raw (first 300 chars): {raw[:300]!r}")
            break

        print(f"    JSON keys   : {list(data.keys())}")
        print(f"    Products    : {len(batch)}")

        if not batch:
            print("    Empty — done.")
            # Show raw to confirm it's a genuine empty response
            print(f"    Raw response: {raw[:200]!r}")
            break

        # Detect if the store is looping (returning same page repeatedly)
        if len(batch) == last_count and page > 2:
            print(f"    WARNING: same count as previous page ({last_count}) — store may be looping, stopping.")
            break
        last_count = len(batch)

        total += len(batch)
        print(f"    Running total: {total}")
        time.sleep(0.5)

    print(f"\n  PAGE RESULT: {page - 1} page(s), {total} products")
    return total


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    store = sys.argv[1]
    base  = base_url(store)

    print(f"Store  : {base}")
    print(f"Target : {base}/products.json")
    print("=" * 60)

    session = requests.Session()
    session.headers.update(HEADERS)

    cursor_total = fetch_cursor(session, base)
    page_total   = fetch_page_based(session, base)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print(f"  Cursor-based : {cursor_total} products")
    print(f"  Page-based   : {page_total} products")
    if page_total > cursor_total:
        print("  → Page-based gets more products. Bot should use ?page=N fallback.")
    elif cursor_total > 0:
        print("  → Cursor-based is working correctly.")
    else:
        print("  → Neither method returned products. Store may be private or unsupported.")
    print("=" * 60)
