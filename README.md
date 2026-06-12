# cata.ai

A Discord bot that monitors Shopify stores for restocks, new drops, price changes, sold out and removed items — pinging subscribed users or roles when stock changes are detected.

## Features

- Polls Shopify `products.json` endpoints on a configurable per-server interval (default 5 minutes)
- Full pagination support — fetches all products regardless of catalog size (cursor-based and page-based fallback)
- Detects restocks, new items, price changes, sold out, and removed products
- Price change alerts — configurable percentage threshold per server (default 10%), toggled per store
- Filtered subscriptions — subscribe by store, product name keywords, and/or size
- Role subscriptions — admins can subscribe a Discord role to receive pings
- Fuzzy size matching: XS / XSMALL / x-small all resolve to the same canonical size
- Multi-keyword name filtering with AND logic (item must contain all keywords)
- Per-store channel routing — send alerts to specific channels, threads, or forums
- Forum support — auto-creates a persistent post per store, replies into it
- Mass drop aggregation — collapses large drops into a single embed and ping (threshold configurable per server)
- Add to Cart buttons on restock, new item, and price change alerts (one per available variant)
- Paginated product catalog with stock status per item
- Auto-discovers Shopify endpoints from human-friendly URLs
- Silently skips password-protected or unreachable stores
- Full multi-server support — each server manages its own stores and settings
- Per-server data and full product cache persisted locally
- Non-blocking logging via QueueHandler — no event loop stalls under load
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

## Linux deployment (Ubuntu Server / Hyper-V)

Shell-script equivalents of every `.bat`/`.ps1` live alongside them (`start.sh`, `stop.sh`,
`restart.sh`, `scripts/watchdog.sh`, `scripts/sync-data.sh`, `scripts/apply-sysctl-tuning.sh`).

First-time setup:

```bash
chmod +x start.sh stop.sh restart.sh scripts/*.sh
python3 -m pip install --break-system-packages -r requirements.txt
```

### Auto-start on boot (systemd)

Install the bot as a systemd service so it starts on boot (after the network is up) and
auto-restarts the watchdog if it ever dies:

```bash
sudo bash scripts/install-service.sh
```

This generates `/etc/systemd/system/catabot.service` from the current path, enables it,
and removes the old `@reboot` crontab entry so the bot can't start twice. Manage it with:

```bash
systemctl status catabot      # check state
systemctl restart catabot     # manual restart
journalctl -u catabot -f      # follow service logs
```

> When running under systemd, control the bot with `systemctl`, not `stop.sh` —
> `systemctl` owns the lifecycle and will restart the watchdog if it exits.

### Surviving a Hyper-V host reboot

The systemd service handles the VM booting, but the **VM itself** must be told to power on
when the Hyper-V host starts. Run once on the **host** (PowerShell, as Administrator):

```powershell
Set-VM -Name "<your-vm-name>" -AutomaticStartAction Start -AutomaticStartDelay 30
```

`Start` always powers the VM on at host boot; the 30s delay staggers startup so the host
finishes initialising first. (Use `Get-VM` to list VM names.)

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
| `/rst store [store]` | Store info, alert toggles, channel, and subscribers |
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
| `/rst-admin interval <seconds>` | Set poll interval (60–600s) |
| `/rst-admin add <name> <url>` | Add a Shopify store |
| `/rst-admin remove [store...]` | Remove up to 5 stores |
| `/rst-admin channel [store] [channel]` | Set a dedicated channel, thread, or forum for a store |
| `/rst-admin subscribe <target> [store] [names] [sizes]` | Create a filtered subscription for a user or role |
| `/rst-admin unsubscribe <id>` | Remove any subscription by ID |
| `/rst-admin recent <store> [channel]` | Post the most recently updated item |
| `/rst-admin alert <store> [channel]` | Send a fake restock alert for testing |
| `/rst-admin export` | Export store list as a shareable code |
| `/rst-admin import <code>` | Import a store list from an export code |
| `/rst-admin price_threshold <percent>` | Set minimum price change % to trigger a price alert (default 10) |
| `/rst-admin aggregate_threshold <count>` | Set how many changed products trigger mass-drop mode (default 20) |

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

Alerts are toggled per store via `/rst store`. All alert types default to off except Restock and New Item.

| Alert | Default | Pings subscribers? | Notes |
|---|---|---|---|
| 🔔 Back in Stock | ✅ On | ✅ Yes | Includes Add to Cart buttons per available variant |
| 🆕 New Item | ✅ On | ✅ Yes | Includes Add to Cart buttons per available variant |
| 📦 Mass Drop | ✅ On | ✅ Yes (once) | Fires when changed items exceed the aggregate threshold — shows a summary |
| 🔴 Sold Out | ❌ Off | ❌ No | |
| 🗑️ Item Removed | ❌ Off | ❌ No | |
| 💲 Price Change | ❌ Off | ✅ Yes | Only fires if price moves ≥ server threshold (default 10%). Includes Add to Cart buttons |

## Project Structure

```
catabot-for-discord/
├── bot.py              # Thin entry point — invokes catabot.app.run()
├── pyproject.toml      # Project metadata, pinned deps, ruff/mypy/pytest config
├── requirements.txt    # Runtime dependencies (used by start scripts)
├── config.toml         # Version and default poll interval
├── .env                # Secret credentials (not committed)
├── catabot/            # Application package
│   ├── runtime.py      #   Paths, AEST timezone, non-blocking rotating logging
│   ├── storage.py      #   JSON persistence (config, state, guild state, caches)
│   ├── shopify.py      #   curl_cffi client, pagination, discovery, URL helpers
│   ├── models.py       #   SearchResult, size normalization, subscription matching
│   ├── embeds.py       #   Alert and listing embed builders
│   ├── views.py        #   discord.ui interactive components
│   ├── cog.py          #   RestockCog — poll loop and all slash commands
│   └── app.py          #   Bot subclass, help UI, restart handling, run()
├── tests/              # pytest suite for the typeable core
├── data/               # Runtime state (local, not committed)
│   ├── bot_state.json
│   ├── stock_state.json
│   ├── products_cache.json
│   └── {guild_id}/state.json
├── .github/workflows/  # CI: ruff + mypy + pytest
└── scripts/
    ├── watchdog.sh / watchdog.ps1     # Auto-restart supervisor (Linux / Windows)
    ├── install-service.sh             # Install the systemd service (Linux)
    ├── apply-sysctl-tuning.sh         # One-time Linux socket tuning
    └── apply-tcp-tuning.ps1           # One-time Windows TCP registry fix
```

## Development

```bash
pip install -e ".[dev]"   # install with dev tools
ruff check catabot tests  # lint
ruff format catabot tests # format
mypy catabot              # type-check
pytest                    # run tests
```

CI runs all four on every push.
