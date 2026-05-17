# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Agent

```bash
# Activate virtualenv (always use myenv — Python 3.11)
source myenv/bin/activate

# Start agent in background
nohup myenv/bin/python3 main.py >> trading_log.txt 2>&1 &

# Start dashboard in background
nohup myenv/bin/python3 dashboard.py >> dashboard_output.log 2>&1 &

# Monitor logs
tail -f trading_log.txt

# Check running processes
pgrep -a python3
```

## Architecture

Two main files: `main.py` (trading agent) and `dashboard.py` (FastAPI web dashboard). No test suite.

**`SentinelAlpha` class** (`main.py`) responsibilities:
- **Wallet init** (`_init_wallet`): Connects to the Coinbase CDP wallet via `CdpEvmWalletProvider`. Persists wallet address to `wallet_data.json`. Uses fixed idempotency key (`sentinel_alpha_main_wallet`).
- **Price fetching** (`fetch_prices`): Calls Coinbase REST API for BTC/USD, ETH/USD, cbETH/USD every 5 minutes. Stores rolling window per pair in pandas DataFrames.
- **Signal generation** (`calculate_z_score`): Z-Score of base/quote price ratio over rolling window. BUY when Z < -threshold, SELL when Z > +threshold.
- **Adaptive threshold** (`_adaptive_threshold`): Scales threshold 0.5×–2.0× based on recent vs. full-window volatility.
- **Cointegration gate** (`_check_cointegration`): Engle-Granger test; pauses trading if pair is not cointegrated (p ≥ COINT_P_THRESHOLD).
- **Trade sizing** (`_scaled_trade_pct`): Linear ramp from TRADE_SIZE_PCT (at threshold) to TRADE_SIZE_MAX_PCT (at threshold + TRADE_SCALE_RAMP).
- **Trade execution** (`execute_trade`): DRY_RUN=true logs shadow trades only. Live mode uses AgentKit `swap` action (cbBTC and cbETH on Base).
- **Aave yield** (`_aave_supply_eth`, `_aave_withdraw_eth`, `_deposit_idle_eth`, `_ensure_eth_available`): Deposits idle ETH to Aave V3 as WETH for yield. Withdraws before BUY trades automatically. `_aave_withdraw_eth` retries once on "Nonce too low" CDP errors (nonce can drift after dropped/failed txns).
- **Notifications** (`_notify`): Sends Telegram messages on trade execution, stop-loss, daily summary, and errors. No-op if token/chat ID not set.
- **Risk controls**: 1-hour cooldown, 50-trade daily cap, daily stop-loss (`check_stop_loss`): if ETH-equiv portfolio drops >5% from day start, trading pauses and the loop continues sleeping every 5 min. If the loss persists for 30 minutes (`STOP_LOSS_CONFIRM_SECS=1800`), the agent calls `sys.exit(1)` — the watchdog will NOT restart it (by design; manual restart required after a true halt). Stop-loss check is skipped entirely when prices or balances are stale to avoid false halts.

**`dashboard.py`** — FastAPI server on port 8000. Reads `trading_log.txt`, `trades.json`, `daily_summary.json`, `price_history.json`. Three-tab UI: Overview, Aave Yield, Log.

## Trading Pairs

| Pair | Base Token | Quote | Networks |
|---|---|---|---|
| BTC/ETH | cbBTC (ERC-20) | native ETH | all |
| cbETH/ETH | cbETH (ERC-20) | native ETH | base-mainnet only |
| wstETH/ETH | wstETH (ERC-20) | native ETH | base-mainnet only |

Capital is split 50/50 between cbETH/ETH and wstETH/ETH.

Capital is split equally between enabled pairs.

## Key Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `CDP_API_KEY_ID` | — | Coinbase CDP API key name |
| `CDP_API_KEY_SECRET` | — | PEM private key (`\n` for newlines) |
| `CDP_WALLET_SECRET` | — | CDP server wallet secret |
| `NETWORK_ID` | `base-sepolia` | `base-sepolia` or `base-mainnet` |
| `DRY_RUN` | `true` | `false` to enable live on-chain swaps |
| `RPC_URL` | — | Override default RPC (recommended: Alchemy/QuickNode) |
| `Z_SCORE_THRESHOLD` | `2.0` | Base signal threshold |
| `TRADE_SCALE_RAMP` | `1.0` | Z excess above threshold to reach max trade size |
| `WINDOW_SIZE_HOURS` | `24` | Rolling window for Z-Score |
| `TRADE_SIZE_PCT` | `0.10` | Base trade size (at threshold) |
| `TRADE_SIZE_MAX_PCT` | `0.40` | Max trade size (at threshold + ramp) |
| `DAILY_STOP_LOSS_PCT` | `0.05` | Max daily drawdown before halting |
| `ADAPTIVE_THRESHOLD_WINDOW` | `12` | Recent price points for vol-regime detection |
| `COINT_P_THRESHOLD` | `0.25` | Max p-value to consider a pair cointegrated |
| `MIN_EFFECTIVE_THRESHOLD` | `1.5` | Hard floor on adaptive Z-score threshold (prevents trading below slippage cost) |
| `SLIPPAGE_GUARD_PCT` | `0.8` | Min expected mean-reversion gain (%) before allowing a trade |
| `STOP_LOSS_WARN_PCT` | `0.04` | Send Telegram warning when daily drawdown exceeds this (before the 5% halt) |
| `COOLDOWN_MINUTES` | `90` | Per-pair cooldown between trades in minutes |
| `AAVE_ENABLED` | `false` | Enable idle ETH yield via Aave V3 |
| `AAVE_POOL_ADDRESS` | `0xA238...` | Aave V3 Pool proxy on Base mainnet |
| `AAVE_AWETH_ADDRESS` | `0xD4a0...` | aWETH token address on Base mainnet |
| `AAVE_ETH_RESERVE` | `0.005` | ETH kept liquid (not deposited) for gas |
| `AAVE_MIN_DEPOSIT` | `0.005` | Min ETH idle before depositing to Aave |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID to receive notifications |

## Token Addresses

`TOKENS` dict in `main.py` maps network → token → address. Addresses differ between mainnet and sepolia.

## Persistent Files

| File | Purpose | Migrate? |
|---|---|---|
| `wallet_data.json` | On-chain wallet address — **critical, do not lose** | Yes |
| `trading_log.txt` | Append-only human-readable log | Optional |
| `trades.json` | JSONL structured trade records | Yes |
| `daily_summary.json` | JSONL daily snapshots | Yes |
| `price_history.json` | Rolling price window cache | Optional |
| `.env` | All credentials and config | Yes (chmod 600) |

## Watchdog & Cron

A cron job runs every 5 minutes to restart the agent if it dies:
```
*/5 * * * * cd /home/jeff/sentinel-alpha-p && pgrep -f 'python3 main.py' > /dev/null || nohup myenv/bin/python3 main.py >> trading_log.txt 2>&1
```

Weekly and monthly analysis reports also run via cron (`analyze.py`).
