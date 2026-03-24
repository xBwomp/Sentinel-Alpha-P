# Sentinel-Alpha Trading Agent

An autonomous mean-reversion trading agent running on the **Coinbase Base Network** (mainnet). Trades cbBTC/ETH and cbETH/ETH pairs using Z-score signals, with Aave V3 yield on idle ETH and Telegram notifications.

---

## Table of Contents
- [Strategy](#strategy)
- [Fresh Server Setup](#fresh-server-setup)
- [Migrating From Another Server](#migrating-from-another-server)
- [Environment Variables](#environment-variables)
- [Telegram Notifications Setup](#telegram-notifications-setup)
- [Aave Yield Setup](#aave-yield-setup)
- [Daily Operations](#daily-operations)
- [Troubleshooting](#troubleshooting)
- [Architecture](#architecture)

---

## Strategy

Mean reversion on BTC/ETH and cbETH/ETH price ratios:
- **BUY** when Z-Score < −threshold (base token undervalued)
- **SELL** when Z-Score > +threshold (base token overvalued)
- Trades are gated by an Engle-Granger cointegration check — paused if the pair is not statistically cointegrated
- Adaptive threshold scales with volatility regime (0.5×–2.0× base)
- Trade size ramps linearly with Z-score from `TRADE_SIZE_PCT` (at threshold) to `TRADE_SIZE_MAX_PCT` (at threshold + `TRADE_SCALE_RAMP`)
- Daily stop-loss halts the agent if portfolio drops more than `DAILY_STOP_LOSS_PCT` from the day's starting value — the loss must persist for 30 minutes before the agent truly halts (to avoid reacting to transient price swings). Once confirmed, the agent calls `sys.exit(1)`; **manual restart is required** — the watchdog will not restart it automatically

---

## Fresh Server Setup

### 1. Prerequisites
```bash
# Python 3.11+ required
python3 --version

# Install pip if needed
sudo apt install python3-pip python3-venv  # Debian/Ubuntu
```

### 2. Clone and set up virtualenv
```bash
git clone https://github.com/xbwomp/sentinel-alpha-p.git
cd sentinel-alpha-p

python3.11 -m venv myenv
source myenv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment
```bash
cp .env.example .env
chmod 600 .env   # restrict to owner only
nano .env        # fill in credentials — see Environment Variables section
```

### 4. Create required data files (if not migrating)
```bash
touch trading_log.txt trades.json daily_summary.json
echo '{}' > wallet_data.json
```

### 5. Start the agent and dashboard
```bash
nohup myenv/bin/python3 main.py >> trading_log.txt 2>&1 &
nohup myenv/bin/python3 dashboard.py >> dashboard_output.log 2>&1 &
```

Dashboard is available at `http://<server-ip>:8000`

### 6. Set up the watchdog cron
```bash
crontab -e
```
Add these lines:
```
# Watchdog: restart agent if not running
*/5 * * * * cd /home/<user>/sentinel-alpha-p && pgrep -f 'python3 main.py' > /dev/null || nohup myenv/bin/python3 main.py >> trading_log.txt 2>&1

# Weekly performance report (Monday 9am)
0 9 * * 1 cd /home/<user>/sentinel-alpha-p && myenv/bin/python3 analyze.py >> analysis_log.txt 2>&1

# Monthly report (1st of month 9am)
0 9 1 * * cd /home/<user>/sentinel-alpha-p && myenv/bin/python3 analyze.py --monthly >> analysis_log.txt 2>&1
```

---

## Migrating From Another Server

### Files to copy (critical)
```bash
# On the old server — pack up everything needed
tar czf sentinel-migrate.tar.gz \
  .env \
  wallet_data.json \
  trades.json \
  daily_summary.json \
  trading_log.txt \
  price_history.json
```

Transfer to new server:
```bash
scp sentinel-migrate.tar.gz user@newserver:/home/<user>/sentinel-alpha-p/
```

On the new server, after completing Fresh Server Setup steps 1–2:
```bash
cd sentinel-alpha-p
tar xzf sentinel-migrate.tar.gz
chmod 600 .env
```

Then continue from step 5.

> **wallet_data.json is critical.** It contains the on-chain wallet address. If lost, the agent will create a new wallet and you will lose access to funds in the old wallet. Back this file up separately.

### Checklist
- [ ] `wallet_data.json` copied and present
- [ ] `.env` copied and `chmod 600` applied
- [ ] `trades.json` and `daily_summary.json` copied (trade history)
- [ ] Virtualenv recreated fresh (`myenv/` — do not copy, reinstall)
- [ ] Watchdog cron set up with correct username/path
- [ ] Agent starts and logs show correct wallet address
- [ ] Telegram notification received on startup
- [ ] Dashboard accessible on port 8000

---

## Environment Variables

All config lives in `.env`. Keep this file `chmod 600`.

### Required
| Variable | Purpose |
|---|---|
| `CDP_API_KEY_ID` | Coinbase CDP API key name (full path: `organizations/.../apiKeys/...`) |
| `CDP_API_KEY_SECRET` | PEM private key — use `\n` for newlines in the single-line format |
| `CDP_WALLET_SECRET` | CDP server wallet secret |

### Network
| Variable | Default | Purpose |
|---|---|---|
| `NETWORK_ID` | `base-sepolia` | `base-mainnet` for live trading |
| `RPC_URL` | — | Custom RPC endpoint — recommended over public nodes (Alchemy/QuickNode free tier works) |
| `DRY_RUN` | `true` | Set `false` to enable live on-chain swaps |

### Strategy
| Variable | Default | Purpose |
|---|---|---|
| `Z_SCORE_THRESHOLD` | `2.0` | Base signal sensitivity |
| `TRADE_SCALE_RAMP` | `1.0` | Z excess to reach max trade size (max at threshold + ramp) |
| `WINDOW_SIZE_HOURS` | `24` | Rolling window for Z-Score calculation |
| `TRADE_SIZE_PCT` | `0.10` | Base trade size at the threshold |
| `TRADE_SIZE_MAX_PCT` | `0.40` | Max trade size at extreme Z-scores |
| `DAILY_STOP_LOSS_PCT` | `0.05` | Portfolio drawdown that halts the agent |
| `ADAPTIVE_THRESHOLD_WINDOW` | `12` | Price points (~1hr) for volatility regime detection |
| `COINT_P_THRESHOLD` | `0.25` | Max cointegration p-value to allow trading |

### Aave Yield
| Variable | Default | Purpose |
|---|---|---|
| `AAVE_ENABLED` | `false` | Enable idle ETH yield via Aave V3 |
| `AAVE_POOL_ADDRESS` | `0xA238Dd80C259a72e81d7e4664a9801593F98d1c5` | Aave V3 Pool on Base mainnet |
| `AAVE_AWETH_ADDRESS` | `0xD4a0e0b9149BCee3C920d2E00b5dE09138fd8bb7` | aWETH token (verify on Basescan: should say "Aave Base WETH") |
| `AAVE_ETH_RESERVE` | `0.001` | ETH kept liquid for gas, not deposited |
| `AAVE_MIN_DEPOSIT` | `0.005` | Min idle ETH before depositing |

### Notifications
| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | — | Your chat ID (see setup below) |

---

## Telegram Notifications Setup

1. Open Telegram, search **@BotFather**, send `/newbot`, follow prompts → receive a bot token
2. Start a chat with your new bot (search by the name you gave it)
3. Get your chat ID — visit this URL in a browser after sending any message to your bot:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
   Find `"chat":{"id":XXXXXXX}` in the response — that number is your chat ID
4. Add to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=<token>
   TELEGRAM_CHAT_ID=<chat_id>
   ```
5. Restart the agent — you'll receive a startup message confirming it works

**Notifications fire on:** agent start, every trade (BUY/SELL), stop-loss trigger, daily summary (midnight), main loop errors.

---

## Aave Yield Setup

Idle ETH earns ~1–3% APY on Aave V3 while waiting for trade signals. No account needed — it uses the same on-chain wallet.

1. Verify the aWETH address is correct: search `0xD4a0e0b9149BCee3C920d2E00b5dE09138fd8bb7` on [Basescan](https://basescan.org) — should show "Aave: aBasWETH Token"
2. Set `AAVE_ENABLED=true` in `.env`
3. Restart the agent

The agent will automatically deposit idle ETH (above the reserve) and withdraw before trades. View live APY at [app.aave.com](https://app.aave.com/?marketName=proto_base_v3).

---

## Daily Operations

```bash
# Check agent is running
pgrep -a python3

# Watch live logs
tail -f trading_log.txt

# Check recent errors
grep -i error trading_log.txt | tail -20

# Manual restart (agent)
pkill -f "python3 main.py"
nohup myenv/bin/python3 main.py >> trading_log.txt 2>&1 &

# Manual restart (dashboard)
pkill -f "dashboard.py"
nohup myenv/bin/python3 dashboard.py >> dashboard_output.log 2>&1 &

# Run performance report
myenv/bin/python3 analyze.py
myenv/bin/python3 analyze.py --monthly
```

---

## Troubleshooting

### Agent stopped and watchdog didn't restart it
If the agent halted due to a confirmed stop-loss, it exits with code 1. The watchdog cron only restarts the agent on unexpected crashes — a deliberate stop-loss halt requires a **manual restart**:
```bash
nohup myenv/bin/python3 main.py >> trading_log.txt 2>&1 &
```
Check the log for `STOP LOSS CONFIRMED` or `STOP LOSS TRIGGERED` to confirm this was the cause.

### Aave withdraw fails with "Nonce too low"
The CDP wallet tracks nonces locally. If a previous transaction was dropped or failed uncleanly, the local nonce can fall behind the on-chain state. The agent retries the transaction once automatically. If it still fails, wait for the next cycle — the nonce resyncs on the next successful transaction.

### Stop-loss triggered but loss looks wrong
The stop-loss measures total portfolio in ETH-equivalent. If `_get_aave_eth_balance()` fails during a cycle (RPC error), the Aave balance reads as 0, making the portfolio appear much smaller than it is. The agent skips the stop-loss check when balances are stale — but if an RPC error resolves mid-cycle, the balance may still appear low for that cycle. Review logs around the trigger time for `[Aave] Error reading aWETH balance` entries.

---

## Architecture

| File | Purpose |
|---|---|
| `main.py` | Single-class trading agent (`SentinelAlpha`) |
| `dashboard.py` | FastAPI dashboard on port 8000 |
| `templates/index.html` | Dashboard Jinja2 template (3 tabs: Overview, Aave, Log) |
| `analyze.py` | CLI performance reporter |
| `wallet_data.json` | Persisted on-chain wallet address |
| `trades.json` | JSONL structured trade log |
| `daily_summary.json` | JSONL daily snapshots (written at midnight) |
| `trading_log.txt` | Unstructured append-only log |
| `price_history.json` | Rolling price window cache |

**Tech stack:** Python 3.11, coinbase-agentkit, web3.py, pandas, statsmodels, FastAPI, Chart.js
