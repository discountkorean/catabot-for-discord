# cata.ai

A Discord bot that monitors Shopify stores for restocks, new drops, sold out and removed items — pinging subscribed users or roles when stock changes are detected.

## Features

- Polls Shopify `products.json` endpoints on a configurable per-server interval
- Detects restocks, new items, sold out, and removed products
- Per-store ping subscriptions for users and roles
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
| `/rst notify [store]` | Toggle restock pings for yourself |
| `/rst store [store]` | Show store info and current subscribers |
| `/rst user [user]` | Show which stores a user is subscribed to |
| `/rst search [query] [stores...]` | Search for a product across stores |

### Admin (`/rst admin`)

> Requires Administrator permission. Hidden from non-admins.

| Command | Description |
|---|---|
| `/rst admin start [channel]` | Start monitoring and set the alert channel |
| `/rst admin stop` | Stop monitoring for this server |
| `/rst admin interval [seconds]` | Set poll interval for this server (60–600s) |
| `/rst admin add [name] [url]` | Add a Shopify store (auto-discovers endpoint) |
| `/rst admin remove [store...]` | Remove up to 5 stores |
| `/rst admin notify [store] [user/role]` | Toggle pings for any user or role |
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
