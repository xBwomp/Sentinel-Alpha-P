# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Agent

```bash
# Local (requires Python 3.11+)
cp .env.example .env   # fill in credentials
pip install -r requirements.txt
python main.py

# Docker (preferred for deployment)
docker-compose up -d --build

# Monitor logs
tail -f trading_log.txt
```

## Architecture

This is a single-file autonomous trading agent (`main.py`). There is no test suite.

**`SentinelAlpha` class** (the only class) has these responsibilities:
- **Wallet init** (`_init_wallet`): Connects to the Coinbase CDP wallet via `CdpEvmWalletProvider`. Persists the wallet address to `wallet_data.json` so the same on-chain wallet is reused across restarts. Uses a fixed idempotency key (`sentinel_alpha_main_wallet`) to prevent duplicate wallet creation.
- **Price fetching** (`fetch_prices`): Calls the public Coinbase REST API for BTC/USD and ETH/USD spot prices every 5 minutes. Stores a rolling window in an in-memory pandas DataFrame.
- **Signal generation** (`calculate_z_score`): Computes the Z-Score of the BTC/ETH price ratio over the rolling window. BUY when Z < -2.0 (BTC undervalued), SELL when Z > +2.0 (BTC overvalued).
- **Trade execution** (`execute_trade`): In `DRY_RUN=true` (default), logs shadow trades only. When live, invokes the AgentKit `swap` action to exchange between native ETH and `cbBTC` (ERC-20 on Base).
- **Risk controls**: 1-hour cooldown between trades, 50-trade daily cap, and a daily stop-loss (halts the agent if ETH balance drops >5% from the day's starting value).

## Key Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `CDP_API_KEY_ID` | — | Coinbase CDP API key name |
| `CDP_API_KEY_SECRET` | — | PEM private key (use `\n` for newlines) |
| `CDP_WALLET_SECRET` | — | CDP server wallet secret |
| `NETWORK_ID` | `base-sepolia` | `base-sepolia` or `base-mainnet` |
| `DRY_RUN` | `true` | `false` to enable live on-chain swaps |
| `Z_SCORE_THRESHOLD` | `2.0` | Signal sensitivity |
| `WINDOW_SIZE_HOURS` | `24` | Rolling window for Z-Score calculation |
| `TRADE_SIZE_PCT` | `0.02` | Fraction of balance per trade |
| `DAILY_STOP_LOSS_PCT` | `0.05` | Max daily drawdown before halting |

## Token Addresses

`TOKENS` dict in `main.py` maps network → token → address. cbBTC and WETH addresses differ between `base-mainnet` and `base-sepolia`. `NATIVE_ETH` uses the sentinel address `0xeeee...eeee`.

## Persistent Files

- `wallet_data.json` — stores the on-chain wallet address between runs; mounted as a Docker volume
- `trading_log.txt` — append-only trade/signal log; mounted as a Docker volume

Both files must exist on the host before running Docker if you want to pre-seed or preserve them across container rebuilds.
