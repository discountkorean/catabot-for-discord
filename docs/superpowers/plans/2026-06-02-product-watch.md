# Product Watch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users watch specific product sizes and receive a DM when a watched size restocks.

**Architecture:** All changes are in `cogs/restock.py`. Watch subscriptions are stored as `{type: "watch"}` entries in the existing guild `subscriptions` list. Watched handles are fetched via `/products/{handle}.js` at each poll cycle and merged into the variant diff. Two UX entry points: a `/rst watch` command and a Watch button on `/rst search` results.

**Tech Stack:** discord.py (discord.ui.View, discord.ui.Button, discord.ui.Select), requests, existing guild state JSON persistence.

---

### Task 1: Extract `_normalize_product_js` helper

The `.js` normalization logic currently lives inline in `_search_suggest_sync`. Extract it so the poll loop can reuse it.

**Files:**
- Modify: `cogs/restock.py` (around line 310 — inside `_search_suggest_sync`)

- [ ] **Step 1: Add the helper function above `_search_suggest_sync`**

Find the line `def _search_suggest_sync(base: str, query: str, limit: int = 10) -> list:` and insert this function immediately before it:

```python
def _normalize_product_js(p: dict) -> dict:
    """Normalize /products/{handle}.js response to products.json shape."""
    for v in p.get("variants", []):
        if isinstance(v.get("price"), int):
            v["price"] = f"{v['price'] / 100:.2f}"
    raw_images = p.get("images", [])
    if raw_images and isinstance(raw_images[0], str):
        p["images"] = [{"src": ("https:" + img if img.startswith("//") else img)} for img in raw_images]
    return p
```

- [ ] **Step 2: Replace the inline normalization in `_search_suggest_sync` with a call to the helper**

Inside `_search_suggest_sync`, find:
```python
            for v in p.get("variants", []):
                if isinstance(v.get("price"), int):
                    v["price"] = f"{v['price'] / 100:.2f}"
            raw_images = p.get("images", [])
            if raw_images and isinstance(raw_images[0], str):
                p["images"] = [{"src": ("https:" + img if img.startswith("//") else img)} for img in raw_images]
            products.append(p)
```

Replace with:
```python
            products.append(_normalize_product_js(p))
```

- [ ] **Step 3: Commit**

```
git add cogs/restock.py
git commit -m "refactor: extract _normalize_product_js helper"
```

---

### Task 2: Add `_fetch_watched_handles_sync` helper

Fetches a set of product handles via `/products/{handle}.js` and returns them as a list of normalized product dicts. Used by the poll loop.

**Files:**
- Modify: `cogs/restock.py` (add after `_normalize_product_js`)

- [ ] **Step 1: Add the function after `_normalize_product_js`**

```python
def _fetch_watched_handles_sync(base: str, handles: list[str]) -> list:
    """Fetch a list of product handles via /products/{handle}.js. Returns normalized product dicts."""
    products = []
    for handle in handles:
        try:
            r = requests.get(f"{base}/products/{handle}.js", headers=HEADERS, timeout=10)
            if r.status_code == 404:
                products.append({"handle": handle, "_removed": True})
                continue
            if not r.ok:
                continue
            products.append(_normalize_product_js(r.json()))
        except Exception as e:
            log.error(f"Failed to fetch watched handle {handle}: {e}")
    return products
```

- [ ] **Step 2: Commit**

```
git add cogs/restock.py
git commit -m "feat: add _fetch_watched_handles_sync helper"
```

---

### Task 3: Integrate watched handles into the poll loop

After the regular deep-fetch for each store, collect all `"watch"` subscriptions for that store across all due guilds, fetch their handles, merge into `current`, and DM users on restock.

**Files:**
- Modify: `cogs/restock.py` — the `poll` method (around line 885)

- [ ] **Step 1: After `current = build_variant_map(products)`, add watched handle fetching**

Find this block inside the poll loop:
```python
            current  = build_variant_map(products)
            previous = self.state.get(url)
```

Replace with:
```python
            current  = build_variant_map(products)

            # Fetch watched handles not already in current
            base = _base_url(url)
            watch_subs = [
                s for gs in self.guilds.values()
                for s in gs.get("subscriptions", [])
                if s.get("type") == "watch" and s.get("store") in gs.get("stores", {})
                and gs["stores"].get(s["store"]) == url
            ]
            watched_handles = list({s["handle"] for s in watch_subs} - {v["handle"] for v in current.values()})
            if watched_handles:
                watched_products = await asyncio.to_thread(_fetch_watched_handles_sync, base, watched_handles)
                removed_handles = set()
                for wp in watched_products:
                    if wp.get("_removed"):
                        removed_handles.add(wp["handle"])
                        continue
                    current.update(build_variant_map([wp]))
                # DM watchers of removed products and delete their watches
                if removed_handles:
                    await self._notify_removed_watches(url, removed_handles)

            previous = self.state.get(url)
```

- [ ] **Step 2: After the diff and `self.state[url] = current`, add watch DM dispatch**

Find `self.state[url] = current` and add after it:
```python
            # DM users whose watched variants just restocked
            if restocked and watch_subs:
                await self._dispatch_watch_dms(url, restocked, watch_subs)
```

- [ ] **Step 3: Add `_notify_removed_watches` method to the cog**

Add this method to the `RestockCog` class (after the `poll` method):

```python
    async def _notify_removed_watches(self, store_url: str, removed_handles: set[str]):
        """DM watchers of removed products and delete their watch subscriptions."""
        for guild_id_str, gs in self.guilds.items():
            store_name = next((n for n, u in gs.get("stores", {}).items() if u == store_url), None)
            if not store_name:
                continue
            watches_to_remove = []
            for s in gs.get("subscriptions", []):
                if s.get("type") != "watch" or s.get("handle") not in removed_handles:
                    continue
                watches_to_remove.append(s["id"])
                try:
                    user = await self.bot.fetch_user(s["target_id"])
                    await user.send(f"👀 A product you were watching has been removed from **{store_name}**: `{s['handle']}`\nYour watch has been automatically deleted.")
                except discord.Forbidden:
                    log.warning(f"Cannot DM user {s['target_id']} about removed watch")
                except Exception as e:
                    log.error(f"Failed to notify user of removed watch: {e}")
            if watches_to_remove:
                gs["subscriptions"] = [s for s in gs["subscriptions"] if s["id"] not in watches_to_remove]
                self.persist(int(guild_id_str))
```

- [ ] **Step 4: Add `_dispatch_watch_dms` method to the cog**

```python
    async def _dispatch_watch_dms(self, store_url: str, restocked: dict, watch_subs: list[dict]):
        """DM users whose watched variant IDs appear in the restocked dict."""
        # Build a flat map of variant_id -> variant info for restocked variants
        restocked_vids = {}
        for variants in restocked.values():
            for v in variants:
                restocked_vids[str(v.get("variant_id", ""))] = v

        store_name = None
        for gs in self.guilds.values():
            store_name = next((n for n, u in gs.get("stores", {}).items() if u == store_url), None)
            if store_name:
                break

        for sub in watch_subs:
            matched = [restocked_vids[vid] for vid in sub.get("variant_ids", []) if vid in restocked_vids]
            if not matched:
                continue
            try:
                user = await self.bot.fetch_user(sub["target_id"])
                embed = make_restock_embed(store_name or sub["store"], store_url, matched)
                embed.title = f"👀 {embed.title}"
                embed.set_footer(text=f"You're watching this product  •  {bot_footer()}")
                await user.send(embed=embed)
                log.info(f"Watch DM sent to {sub['target_id']} for {sub['handle']}")
            except discord.Forbidden:
                log.warning(f"DMs disabled for user {sub['target_id']}, removing watch {sub['id']}")
                for gs in self.guilds.values():
                    before = len(gs.get("subscriptions", []))
                    gs["subscriptions"] = [s for s in gs.get("subscriptions", []) if s["id"] != sub["id"]]
                    if len(gs["subscriptions"]) != before:
                        self.persist(int(list(self.guilds.keys())[list(self.guilds.values()).index(gs)]))
            except Exception as e:
                log.error(f"Failed to send watch DM to {sub['target_id']}: {e}")
```

- [ ] **Step 5: Commit**

```
git add cogs/restock.py
git commit -m "feat: integrate watched handles into poll loop with DM alerts"
```

---

### Task 4: Build `WatchSizePicker` UI

A `discord.ui.View` that shows all product variants as toggle buttons, plus Confirm and Cancel. Used by both `/rst watch` and the Watch button on search results.

**Files:**
- Modify: `cogs/restock.py` (add near other View classes, after `SearchPaginator`)

- [ ] **Step 1: Add `WatchSizePicker` class after `SearchPaginator`**

```python
class WatchSizePicker(discord.ui.View):
    """Size-picker UI for watching a product. Shows all variants as toggle buttons."""

    def __init__(self, cog, guild_id: int, store_name: str, store_url: str, product: dict):
        super().__init__(timeout=120)
        self.cog        = cog
        self.guild_id   = guild_id
        self.store_name = store_name
        self.store_url  = store_url

        self.product_title = product.get("title", "Unknown")
        self.handle        = product.get("handle", "")
        base               = _base_url(store_url)
        self.product_url   = f"{base}/products/{self.handle}"
        self.image_url     = (product.get("images") or [{}])[0].get("src")

        self.variants: list[dict] = product.get("variants", [])
        self.selected: set[str]   = set()

        # Add one button per variant (up to 20 to leave room for Confirm/Cancel row)
        for v in self.variants[:20]:
            vid       = str(v["id"])
            avail     = v.get("available", False)
            label     = f"{v.get('title', vid)} {'✅' if avail else '🔴'}"
            btn       = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                custom_id=f"watch_size_{vid}",
            )
            btn.callback = self._make_toggle(vid)
            self.add_item(btn)

        self.confirm_btn = discord.ui.Button(
            label="Confirm", style=discord.ButtonStyle.success, disabled=True, row=4
        )
        self.confirm_btn.callback = self._confirm
        self.add_item(self.confirm_btn)

        cancel_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger, row=4)
        cancel_btn.callback = self._cancel
        self.add_item(cancel_btn)

    def _make_toggle(self, vid: str):
        async def toggle(interaction: discord.Interaction):
            if vid in self.selected:
                self.selected.discard(vid)
            else:
                self.selected.add(vid)
            # Update button styles
            for item in self.children:
                if isinstance(item, discord.ui.Button) and item.custom_id == f"watch_size_{vid}":
                    item.style = discord.ButtonStyle.primary if vid in self.selected else discord.ButtonStyle.secondary
            self.confirm_btn.disabled = len(self.selected) == 0
            await interaction.response.edit_message(view=self)
        return toggle

    async def _confirm(self, interaction: discord.Interaction):
        selected_variants = [v for v in self.variants if str(v["id"]) in self.selected]
        variant_ids    = [str(v["id"]) for v in selected_variants]
        variant_titles = [v.get("title", str(v["id"])) for v in selected_variants]

        # Check for duplicate watch
        gs = self.cog._guild(self.guild_id)
        existing = next((s for s in gs.get("subscriptions", [])
                         if s.get("type") == "watch"
                         and s.get("handle") == self.handle
                         and s.get("target_id") == interaction.user.id
                         and sorted(s.get("variant_ids", [])) == sorted(variant_ids)), None)
        if existing:
            await interaction.response.edit_message(
                content=f"You already have an identical watch `[{existing['id']}]`.",
                view=None, embed=None
            )
            return

        sub = {
            "type":           "watch",
            "id":             str(uuid.uuid4())[:8],
            "target_id":      interaction.user.id,
            "store":          self.store_name,
            "handle":         self.handle,
            "variant_ids":    variant_ids,
            "variant_titles": variant_titles,
        }
        gs.setdefault("subscriptions", []).append(sub)
        self.cog.persist(self.guild_id)

        # Seed state so already-available variants don't re-alert
        state_key = self.store_url
        if state_key in self.cog.state:
            for v in selected_variants:
                vid = str(v["id"])
                if vid not in self.cog.state[state_key]:
                    self.cog.state[state_key][vid] = {
                        "available":     v.get("available", False),
                        "title":         self.product_title,
                        "variant_title": v.get("title", ""),
                        "price":         str(v.get("price", "0.00")),
                        "handle":        self.handle,
                        "image_url":     self.image_url,
                    }
            save_state(self.cog.state)

        sizes_str = ", ".join(variant_titles)
        await interaction.response.edit_message(
            content=f"👀 Watching **{self.product_title}** ({sizes_str}) at **{self.store_name}**. You'll get a DM when it restocks.",
            embed=None, view=None
        )

        # DM for already-in-stock variants
        in_stock = [v for v in selected_variants if v.get("available")]
        if in_stock:
            try:
                sizes_in_stock = ", ".join(v.get("title", "") for v in in_stock)
                await interaction.user.send(
                    f"👀 Heads up — **{sizes_in_stock}** of **{self.product_title}** is already in stock at **{self.store_name}**:\n{self.product_url}"
                )
            except discord.Forbidden:
                pass

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Watch cancelled.", embed=None, view=None)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=self.product_title, url=self.product_url, color=0x5865F2,
            description="Select the sizes you want to watch, then click **Confirm**.",
        )
        if self.image_url:
            embed.set_thumbnail(url=self.image_url)
        embed.add_field(name="Store", value=self.store_name, inline=True)
        embed.set_footer(text=bot_footer())
        return embed
```

- [ ] **Step 2: Commit**

```
git add cogs/restock.py
git commit -m "feat: add WatchSizePicker UI"
```

---

### Task 5: Build `WatchProductSelect` UI

A `discord.ui.View` with a Select menu for picking a product from suggest results. On selection, transitions to `WatchSizePicker`.

**Files:**
- Modify: `cogs/restock.py` (add after `WatchSizePicker`)

- [ ] **Step 1: Add `WatchProductSelect` class**

```python
class WatchProductSelect(discord.ui.View):
    """Product pick-list shown after /rst watch search results."""

    def __init__(self, cog, guild_id: int, store_name: str, store_url: str, products: list[dict]):
        super().__init__(timeout=120)
        self.cog        = cog
        self.guild_id   = guild_id
        self.store_name = store_name
        self.store_url  = store_url
        self.products   = {p["handle"]: p for p in products}

        options = [
            discord.SelectOption(label=p.get("title", p["handle"])[:100], value=p["handle"])
            for p in products[:10]
        ]
        select = discord.ui.Select(placeholder="Choose a product…", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        handle  = interaction.data["values"][0]
        product = self.products[handle]
        picker  = WatchSizePicker(self.cog, self.guild_id, self.store_name, self.store_url, product)
        await interaction.response.edit_message(embed=picker.build_embed(), view=picker)
```

- [ ] **Step 2: Commit**

```
git add cogs/restock.py
git commit -m "feat: add WatchProductSelect UI"
```

---

### Task 6: Add `/rst watch` command

**Files:**
- Modify: `cogs/restock.py` (add as a new `@tracker.command` in the cog, near `restock_search`)

- [ ] **Step 1: Add the command after `restock_search`**

```python
    @tracker.command(name="watch", description="Watch a product for restocks — get a DM when your size drops")
    @app_commands.describe(store_name="Store to search", query="Product name or keyword")
    @app_commands.autocomplete(store_name=_store_autocomplete)
    async def tracker_watch(self, interaction: discord.Interaction, store_name: str, query: str):
        await interaction.response.defer(ephemeral=True)
        stores = self._guild_stores(interaction.guild_id)

        if store_name not in stores:
            await interaction.followup.send(f"❌ **{store_name}** is not a monitored store.", ephemeral=True)
            return

        store_url = stores[store_name]
        base      = _base_url(store_url)
        products  = await search_suggest(base, query)

        if not products:
            await interaction.followup.send(f"No products found matching **{query}** in **{store_name}**.", ephemeral=True)
            return

        if len(products) == 1:
            # Skip the pick list and go straight to size picker
            picker = WatchSizePicker(self, interaction.guild_id, store_name, store_url, products[0])
            await interaction.followup.send(embed=picker.build_embed(), view=picker, ephemeral=True)
        else:
            view = WatchProductSelect(self, interaction.guild_id, store_name, store_url, products)
            await interaction.followup.send("Select a product to watch:", view=view, ephemeral=True)
```

- [ ] **Step 2: Commit**

```
git add cogs/restock.py
git commit -m "feat: add /rst watch command"
```

---

### Task 7: Add Watch button to `SearchPaginator`

**Files:**
- Modify: `cogs/restock.py` — `SearchPaginator` class (around line 51)

- [ ] **Step 1: Add `store_name`, `store_url`, and `cog` to `SearchPaginator.__init__`**

Find:
```python
class SearchPaginator(discord.ui.View):
    def __init__(self, results: list[SearchResult]):
        super().__init__(timeout=120)
        self.results = results
        self.page    = 0
        self._update_buttons()
```

Replace with:
```python
class SearchPaginator(discord.ui.View):
    def __init__(self, results: list[SearchResult], cog=None, guild_id: int = None):
        super().__init__(timeout=120)
        self.results  = results
        self.cog      = cog
        self.guild_id = guild_id
        self.page     = 0
        self._update_buttons()
```

- [ ] **Step 2: Add the Watch button to `SearchPaginator`**

Add this method to `SearchPaginator` after `next_btn`:
```python
    @discord.ui.button(label="👀 Watch", style=discord.ButtonStyle.primary)
    async def watch_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog or not self.guild_id:
            await interaction.response.send_message("Watch unavailable here.", ephemeral=True)
            return
        r         = self.results[self.page]
        stores    = self.cog._guild_stores(self.guild_id)
        store_url = stores.get(r.store_name, "")
        # Fetch full product data via .js for size picker
        base    = _base_url(store_url)
        product = await asyncio.to_thread(
            lambda: _normalize_product_js(
                requests.get(f"{base}/products/{r.handle}.js", headers=HEADERS, timeout=10).json()
            )
        )
        picker = WatchSizePicker(self.cog, self.guild_id, r.store_name, store_url, product)
        await interaction.response.send_message(embed=picker.build_embed(), view=picker, ephemeral=True)
```

- [ ] **Step 3: Pass `cog` and `guild_id` when constructing `SearchPaginator` in `restock_search`**

Find:
```python
        paginator = SearchPaginator(results)
```

Replace with:
```python
        paginator = SearchPaginator(results, cog=self, guild_id=interaction.guild_id)
```

- [ ] **Step 4: Commit**

```
git add cogs/restock.py
git commit -m "feat: add Watch button to search results"
```

---

### Task 8: Update `/rst subscriptions` to show watch entries

**Files:**
- Modify: `cogs/restock.py` — `tracker_subscriptions` (around line 1114)

- [ ] **Step 1: Update the subscriptions filter to include watch type**

Find:
```python
        subs = [s for s in gs["subscriptions"] if s["type"] == "user" and s["target_id"] == target.id]
```

Replace with:
```python
        subs = [s for s in gs["subscriptions"]
                if s["type"] in ("user", "watch") and s["target_id"] == target.id]
```

- [ ] **Step 2: Update the embed field rendering to handle both types**

Find:
```python
        for s in subs:
            lines = [
                f"**Store:** {', '.join(s['stores']) if s['stores'] else 'All'}",
                f"**Names:** {', '.join(s['names']) if s['names'] else 'Any'}",
                f"**Sizes:** {', '.join(s['sizes']) if s['sizes'] else 'Any'}",
            ]
            embed.add_field(name=f"`{s['id']}`", value="\n".join(lines), inline=False)
```

Replace with:
```python
        for s in subs:
            if s["type"] == "watch":
                sizes = ", ".join(s.get("variant_titles", [])) or "All sizes"
                lines = [f"👀 **[{s['store']}]** {s['handle']} ({sizes})"]
            else:
                lines = [
                    f"**Store:** {', '.join(s['stores']) if s['stores'] else 'All'}",
                    f"**Names:** {', '.join(s['names']) if s['names'] else 'Any'}",
                    f"**Sizes:** {', '.join(s['sizes']) if s['sizes'] else 'Any'}",
                ]
            embed.add_field(name=f"`{s['id']}`", value="\n".join(lines), inline=False)
```

- [ ] **Step 3: Commit**

```
git add cogs/restock.py
git commit -m "feat: show watch subscriptions in /rst subscriptions"
```

---

### Task 9: Bump version to 3.3.0

**Files:**
- Modify: `config.toml`

- [ ] **Step 1: Update version**

In `config.toml`, change:
```toml
version = "3.2.3"
```
to:
```toml
version = "3.3.0"
```

- [ ] **Step 2: Commit and push**

```
git add config.toml
git commit -m "Bump version to 3.3.0, add product watch feature"
git push
```

---

## Self-Review

**Spec coverage check:**
- ✅ Watch subscription data model — Task 4 (`_confirm` in `WatchSizePicker`)
- ✅ Poll integration: fetch watched handles — Task 3
- ✅ Poll integration: merge into current — Task 3 (`current.update(build_variant_map([wp]))`)
- ✅ DM on restock — Task 3 (`_dispatch_watch_dms`)
- ✅ 404 / removed product DM + auto-delete — Task 3 (`_notify_removed_watches`)
- ✅ DMs disabled → delete watch — Task 3 (`_dispatch_watch_dms` Forbidden handler)
- ✅ No re-DM for already-available variants (transition detection) — Task 3 (uses existing restocked dict which is unavailable→available only)
- ✅ One-time "already in stock" DM on watch creation — Task 4 (`_confirm`)
- ✅ State seeding on watch creation — Task 4 (`_confirm`)
- ✅ `/rst watch` command — Task 6
- ✅ Watch button on search results — Task 7
- ✅ Size picker: all variants active, availability suffix — Task 4
- ✅ Toggle selection, Confirm disabled until selection — Task 4
- ✅ Duplicate watch check — Task 4
- ✅ `/rst subscriptions` shows watches — Task 8
- ✅ `/rst unsubscribe` works as-is (no changes needed) — confirmed, uses `s["id"]` match

**Type consistency check:**
- `variant_ids` is `list[str]` throughout (cast with `str(v["id"])` at creation, checked with `str` keys in `_dispatch_watch_dms`)
- `store_url` passed consistently from guild stores dict to all helpers
- `_base_url(store_url)` called wherever `.js` endpoint needed

**Placeholder scan:** None found.
