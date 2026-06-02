# cata.ai

A Discord bot that monitors Shopify stores for restocks, new drops, sold out and removed items — pinging subscribed users or roles when stock changes are detected.

## Features

- Polls Shopify `products.json` endpoints on a configurable per-server interval
- Detects restocks, new items, sold out, and removed products
- Filtered subscriptions — subscribe by store, product name keywords, and/or size
- Fuzzy size matching: XS / XSMALL / x-small / extra-small all match the same
- Multi-keyword name filtering with AND logic (item must contain all keywords)
- Auto-discovers Shopify endpoints from human-friendly URLs
- Silently skips password-protected or unreachable stores
- Full multi-server support — each server manages its own stores and settings
- Per-server data persisted to a private git repo, pulled on startup

## Setup

### Requirements

- Python 3.11+
- Dependencies listed in `requirements.txt` (`discord.py`, `requests`, `python-dotenv`, `tzdata`)

```
pip install -r requirements.txt
```

On Windows, `start.bat` pulls the latest data and starts the bot automatically.

### Configuration

1. Create a `.env` file in the project root:

```env
DISCORD_TOKEN=your_token_here
```

2. `config.toml` controls the bot version and default poll interval. Stores are managed per-server via Discord commands.

3. Run the bot:

```
start.bat         # Windows (recommended)
python bot.py     # Direct
```

## Commands

### Public (`/rst`)

| Command | Description |
|---|---|
| `/rst status` | Show tracker state, interval, and monitored stores |
| `/rst subscribe [store] [names] [sizes]` | Subscribe to restock alerts with optional filters |
| `/rst unsubscribe <id>` | Remove one of your subscriptions by ID |
| `/rst subscriptions [user]` | List your active subscriptions |
| `/rst store [store]` | Show store info and all current subscribers |
| `/rst user [user]` | Show a user's subscriptions |
| `/rst search [query] [stores...]` | Search for a product across stores |

#### `/rst subscribe` filter options

All parameters are optional. Omitting all creates a catch-all subscription for every item at every store.

| Parameter | Example | Behaviour |
|---|---|---|
| `store` | `store:HOUND ARCHIVES` | Only notify for items from this store |
| `names` | `names:black,zip-up` | Item title must contain **all** keywords (AND logic) |
| `sizes` | `sizes:small,xs` | Item must match **any** of the listed sizes (OR logic) |

Size matching is fuzzy — `small`, `SMALL`, `Sm`, and `S` all resolve to the same canonical size.

### Admin (`/rst admin`)

> Requires Administrator permission. Hidden from non-admins.

| Command | Description |
|---|---|
| `/rst admin start [channel]` | Start monitoring and set the alert channel |
| `/rst admin stop` | Stop monitoring for this server |
| `/rst admin interval [seconds]` | Set poll interval for this server (60–600s) |
| `/rst admin add [name] [url]` | Add a Shopify store (auto-discovers endpoint) |
| `/rst admin remove [store...]` | Remove up to 5 stores |
| `/rst admin subscribe [user/role] [store] [names] [sizes]` | Create a filtered subscription for any user or role |
| `/rst admin unsubscribe <id>` | Remove any subscription by ID |
| `/rst admin recent [store] [channel]` | Post the most recently updated item |
| `/rst admin alert [store] [channel]` | Send a fake restock alert for testing |
| `/rst admin export` | Export store list as a shareable code |
| `/rst admin import [code]` | Import a store list from an export code |

### Help

| Command | Description |
|---|---|
| `/help general` | All public commands |
| `/help rst` | Detailed /rst command list |
| `/help admin` | Admin commands (admin only) |
| `/help rst-admin` | /rst admin commands (admin only) |

### Bot

| Command | Description |
|---|---|
| `/restart` | Restart the bot process (admin only) |

## Alert Types

| Alert | Pings subscribers? |
|---|---|
| 🔔 Back in Stock | ✅ Yes |
| 🆕 New Item | ✅ Yes |
| 🔴 Sold Out | ❌ No |
| 🗑️ Item Removed | ❌ No |

## Project Structure

```
catabot-for-discord/
├── bot.py              # Bot entry point
├── requirements.txt    # Python dependencies
├── config.toml         # Version and default poll interval
├── .env                # Secret credentials (not committed)
├── start.bat           # Pull data + start bot
├── stop.bat            # Sync data + stop bot
├── restart.bat         # Sync data + restart bot
├── cogs/
│   └── restock.py      # All monitoring logic and commands
├── data/               # Runtime state (separate private repo)
│   ├── bot_state.json
│   ├── stock_state.json
│   └── {guild_id}/
│       └── state.json
└── scripts/
    ├── bot.ps1         # Windows start/stop/restart helper
    └── sync-data.bat   # Manually sync both repos
```
