# Product Watch Feature — Design Spec
**Date:** 2026-06-02  
**Status:** Approved

---

## Overview

Users can watch specific product sizes (variants) on any monitored Shopify store. When a watched size restocks, the bot DMs the user directly. Watched products are fetched individually at each poll interval via `/products/{handle}.js`, supplementing the regular deep-fetch scan.

---

## Data Model

A `"watch"` subscription entry stored in the guild's `subscriptions` list (same JSON as existing user/role subscriptions):

```json
{
  "type": "watch",
  "id": "uuid",
  "target_id": 123456789,
  "store": "BANDED TOGETHER",
  "handle": "cashmere-fitted-crew-neck-espresso",
  "variant_ids": ["42509597409367", "42509597409368"],
  "variant_titles": ["S", "M"]
}
```

- `target_id` — Discord user ID (for DMing)
- `handle` — Shopify product handle, used to fetch `/products/{handle}.js`
- `variant_ids` — list of variant IDs to monitor; poll checks these against previous state
- `variant_titles` — stored for display only in `/rst subscriptions`

Persisted automatically via existing `save_state` / `load_state` since it lives in the guild subscriptions list. No new files or keys needed.

---

## Poll Integration

At each poll cycle, after the regular deep-fetch for a store:

1. Collect all `"watch"` subscriptions across guilds for the current store URL
2. Dedupe by handle — a handle watched by multiple users is fetched once
3. Fetch each handle via `GET {base}/products/{handle}.js`
4. Normalize `.js` response to `products.json` shape (cents → decimal string, images → `[{src}]`, `https:` prefix for protocol-relative URLs)
5. Merge those variants into `current` before the diff — they participate in normal restock/sold-out/removed detection
6. After diff: for each restocked variant, check if any watch subscription matches `variant_id` — if so, DM that user

**Restock detection:** uses the existing `unavailable → available` transition check. Watches only fire on transition, not when a variant remains available between polls.

**404 / removed products:** if `/products/{handle}.js` returns 404, DM the watcher: "A product you were watching has been removed from **{store}**." Auto-delete the watch subscription.

**DMs disabled:** log and skip silently — no crash, no retry.

---

## Watch Creation Flow

### Path 1 — `/rst watch [store] [query]`

1. Run `search_suggest(base, query)` → up to 10 results
2. If no results → ephemeral error
3. Show embed with a **select menu** (dropdown) of matching products (title + store)
4. User selects a product → embed updates to size picker
5. Size picker shows **all variants as active buttons** regardless of stock, with availability suffix: `S ✅` (in stock) or `S 🔴` (sold out)
6. User clicks sizes to toggle selection (highlighted when selected)
7. User clicks **Confirm** → watch saved
8. If any selected variant is currently in stock → send one-time DM: "👀 Heads up — **{size}** is already in stock at **{store}**: {product_url}"
9. Seed those variants as `available` in state so the next poll does not re-DM

### Path 2 — Watch button on `/rst search` results

- A **👀 Watch** button added to the `SearchPaginator` action row (alongside Prev/Next)
- Clicking it skips straight to step 5 (size picker) for the currently displayed product
- Same flow from step 5 onward

---

## UI Details

**Select menu (product pick list):** up to 10 options, one per suggest result. Each option label is the product title, description is the store name.

**Size picker embed:**
- Title: product title (linked to product URL)
- Thumbnail: product image
- Body: one button per variant, all active
  - Label: `S ✅` / `S 🔴`
  - Toggles highlighted state on click
- Final row: **Confirm** (disabled until at least one size selected) + **Cancel**
- Timeout: 120s — if no interaction, edit embed to "Watch cancelled (timed out)"

**Watch button on search results:** placed in the existing Prev/Next row. Row has 5 slots — Prev + Next use 2, Watch uses 1, leaving 2 spare.

---

## `/rst subscriptions` Display

Watch entries listed as:
```
👀 [BANDED TOGETHER] Cashmere Fitted Crew Neck - Espresso (S, M) — ID: abc123
```

---

## Removal

`/rst unsubscribe <id>` — works as-is, no changes needed. Works for both user/role and watch subscriptions.

---

## Alert DM Format

Reuses `make_restock_embed` with a personal header:

> 👀 **A product you're watching just restocked!**
> {standard restock embed for the specific variant(s)}

Sent via `user.send(...)`. If `discord.Forbidden` (DMs disabled), log and delete the watch to avoid repeated failed attempts.

---

## Commands Summary

| Command | Description |
|---|---|
| `/rst watch [store] [query]` | Search for a product and watch specific sizes |

No changes to: `/rst search`, `/rst subscriptions`, `/rst unsubscribe`.

---

## Out of Scope

- Cross-guild watches (watches are per-guild)
- Role watches (watches are user-only, alerts go to DM)
- Admin-created watches on behalf of users
- Watch limits per user (can revisit if abuse occurs)
