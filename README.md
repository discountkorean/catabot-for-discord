# cata.ai

A Discord bot that monitors Shopify stores for restocks and new item drops, and pings subscribed users or roles when stock changes are detected.

## Features

- Polls Shopify `products.json` endpoints on a configurable interval
- Detects restocks (out of stock → in stock) and new items
- Per-store ping subscriptions for users and roles
- Silently skips password-protected or unreachable stores
- All config persists across restarts

## Setup

### Requirements

- Python 3.11+
- Dependencies listed in `requirements.txt` (`discord.py`, `requests`, `python-dotenv`, `tzdata`)

On Windows, `start.bat` installs dependencies automatically before launching the bot. To install manually:

```
pip install -r requirements.txt
```

### Configuration

1. Create a `.env` file in the project root with your bot token:

```env
DISCORD_TOKEN=your_token_here
```

2. Edit `config.toml` to set your stores and poll interval:

```toml
[bot]
version = "1.0.1"

[monitor]
poll_interval = 300  # seconds

[stores]
"STORE NAME" = "https://store.com/products.json?limit=1000"
```

3. Run the bot:

On Windows, double-click `start.bat` — it pulls the latest data, installs dependencies, and starts the bot in the background.

To run directly:

```
python bot.py
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
| `/help` | List all available commands |

### Admin (`/rst admin`)

> Requires Administrator permission. Hidden from non-admins.

| Command | Description |
|---|---|
| `/rst admin start [channel]` | Start monitoring and set the alert channel |
| `/rst admin stop` | Stop monitoring |
| `/rst admin interval [seconds]` | Set poll interval (60–600s) |
| `/rst admin add [name] [url]` | Add a store at runtime |
| `/rst admin remove [store...]` | Remove up to 5 stores |
| `/rst admin notify [store] [user/role]` | Toggle pings for any user or role |
| `/rst admin recent [store] [channel]` | Post the most recently updated item |
| `/rst admin alert [store] [channel]` | Send a fake restock alert for testing |
| `/rst admin help` | List all admin commands |
| `/restart` | Restart the bot process |

## Project Structure

```
catabot-for-discord/
├── bot.py              # Bot entry point
├── requirements.txt    # Python dependencies
├── config.toml         # Stores, poll interval, version
├── .env                # Secret credentials (not committed)
├── start.bat           # Windows launcher (installs deps + starts bot)
├── stop.bat            # Windows stop helper
├── restart.bat         # Windows restart helper
├── cogs/
│   └── restock.py      # All monitoring logic and commands
├── data/               # Runtime state (gitignored)
└── scripts/
    └── bot.ps1         # Windows start/stop/restart helper
```
