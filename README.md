# cata.ai

A Discord bot that monitors Shopify stores for restocks, new drops, sold out and removed items — pinging subscribed users or roles when stock changes are detected.

## Features

- Polls Shopify `products.json` endpoints on a configurable per-server interval (default 5 minutes)
- Full pagination support — fetches all products regardless of catalog size (cursor-based and page-based fallback)
- Detects restocks, new items, sold out, and removed products
- Filtered subscriptions — subscribe by store, product name keywords, and/or size
- Role subscriptions — admins can subscribe a Discord role to receive pings
- Fuzzy size matching: XS / XSMALL / x-small all resolve to the same canonical size
- Multi-keyword name filtering with AND logic (item must contain all keywords)
- Per-store channel routing — send alerts to specific channels, threads, or forums
- Forum support — auto-creates a persistent post per store, replies into it
- Mass drop aggregation — collapses large drops (>5 items) into a single embed and ping
- Paginated product catalog with stock status per item
- Auto-discovers Shopify endpoints from human-friendly URLs
- Silently skips password-protected or unreachable stores
- Full multi-server support — each server manages its own stores and settings
- Per-server data and full product cache persisted locally
- Watchdog auto-restarts the bot on crash with smart backoff for socket exhaustion

## Setup

### Requirements

- Python 3.11+
- Dependencies listed in `requirements.txt` (`discord.py`, `requests`, `python-dotenv`, `tzdata`)

```
pip install -r requirements.txt
```

### Configuration

1. Create a `.env` file in the project root:

```env
DISCORD_TOKEN=your_token_here
```

2. `config.toml` controls the bot version and default poll interval. Stores are managed per-server via Discord commands.

3. Run the bot:

```
watchdog.bat      # Windows (recommended — auto-restarts on crash)
python bot.py     # Direct
```

### Windows TCP tuning (recommended for 24/7 operation)

Run once as Administrator to prevent socket exhaustion (`WinError 10055`) on long-running deployments:

```
scripts\apply-tcp-tuning.ps1
```

Reboot after running. Sets `TcpTimedWaitDelay=30s` and expands the ephemeral port range.

## Commands

### Help

| Command | Description |
|---|---|
| `/help` | All commands — admin pages hidden from non-admins |
| `/rst help` | Detailed `/rst` command reference |
| `/rst-admin help` | Detailed `/rst-admin` command reference (admin only) |

### Public (`/rst`)

| Command | Description |
|---|---|
| `/rst status` | Tracker state, stores, and per-store channel overrides |
| `/rst subscribe [store] [names] [sizes]` | Subscribe to alerts with optional filters |
| `/rst unsubscribe <id>` | Remove a subscription by ID |
| `/rst subscriptions [user]` | List your active subscriptions |
| `/rst store [store]` | Store info, alert channel, and subscribers |
| `/rst user [user]` | A user's subscriptions |
| `/rst catalog [store]` | Browse all products with stock status and price |
| `/rst search [query] [stores...]` | Search for a product across stores |

#### `/rst subscribe` filter options

All parameters optional — omitting all subscribes to everything.

| Parameter | Example | Behaviour |
|---|---|---|
| `store` | `store:HOUND ARCHIVES` | Only notify for items from this store |
| `names` | `names:black,zip-up` | Item must contain **all** keywords (AND logic) |
| `sizes` | `sizes:small,xs` | Item must match **any** size (OR logic, fuzzy) |

#### `/rst catalog` legend

🟢 All sizes in stock — 🟠 Some sizes available — 🔴 Sold out

### Admin (`/rst-admin`)

> Requires Administrator permission.

| Command | Description |
|---|---|
| `/rst-admin start [channel]` | Start monitoring and set the default alert channel |
| `/rst-admin stop` | Stop monitoring for this server |
| `/rst-admin interval [seconds]` | Set poll interval (60–600s) |
| `/rst-admin add [name] [url]` | Add a Shopify store |
| `/rst-admin remove [store...]` | Remove up to 5 stores |
| `/rst-admin channel [store] [channel]` | Set a dedicated channel, thread, or forum for a store |
| `/rst-admin subscribe [target] [store] [names] [sizes]` | Create a filtered subscription for a user or role |
| `/rst-admin unsubscribe <id>` | Remove any subscription by ID |
| `/rst-admin recent [store] [channel]` | Post the most recently updated item |
| `/rst-admin alert [store] [channel]` | Send a fake restock alert for testing |
| `/rst-admin export` | Export store list as a shareable code |
| `/rst-admin import [code]` | Import a store list from an export code |

#### Per-store channel routing

`/rst-admin channel` accepts a text channel, thread, or forum:
- **Text channel / Thread** — alerts sent directly
- **Forum** — bot creates a `{Store} Updates` post on first alert and replies into it

Omit the channel argument to revert a store to the server default.

### Bot

| Command | Description |
|---|---|
| `/help` | Show command reference |
| `/restart` | Restart the bot process (admin only) |

## Alert Types

| Alert | Pings subscribers? | Notes |
|---|---|---|
| 🔔 Back in Stock | ✅ Yes | |
| 🆕 New Item | ✅ Yes | |
| 📦 Mass Drop | ✅ Yes (once) | Triggered when >20 items drop in one cycle — shows a summary instead of listing all products |
| 🔴 Sold Out | ❌ No | |
| 🗑️ Item Removed | ❌ No | |

## Project Structure

```
catabot-for-discord/
├── bot.py              # Bot entry point and help pages
├── requirements.txt    # Python dependencies
├── config.toml         # Version and default poll interval
├── .env                # Secret credentials (not committed)
├── watchdog.bat        # Start bot with auto-restart watchdog
├── stop.bat            # Stop the bot
├── cogs/
│   └── restock.py      # All monitoring logic and commands
├── data/               # Runtime state (local, not committed)
│   ├── bot_state.json
│   ├── stock_state.json
│   ├── products_cache.json
│   └── {guild_id}/
│       └── state.json
└── scripts/
    ├── watchdog.ps1        # PowerShell watchdog (launched by watchdog.bat)
    ├── apply-tcp-tuning.ps1 # One-time Windows TCP registry fix
    └── check_pagination.py  # Diagnostic — test Shopify pagination for a store URL
```
