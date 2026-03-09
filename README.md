# Sentinel-Alpha Trading Agent

Sentinel-Alpha is an autonomous Mean Reversion trading agent built for the **Coinbase Base Network** (Base Sepolia Testnet). It utilizes the **Coinbase AgentKit SDK** to monitor BTC/ETH price ratios and execute automated "Shadow Trades" based on statistical deviations.

## Description

The agent implements a quantitative **Mean Reversion strategy** by calculating a rolling **Z-Score** of the BTC/ETH price ratio over a 24-hour window. 

### Core Logic:
- **Signal Generation**:
  - **BUY**: Triggered when the Z-Score falls below the threshold (BTC is undervalued relative to ETH).
  - **SELL**: Triggered when the Z-Score rises above the threshold (BTC is overvalued relative to ETH).
- **Adaptive Threshold**: The Z-score trigger scales with the current volatility regime — raised during high-volatility periods (noise reduction) and lowered during calm periods (higher sensitivity). Clamped to [0.5×, 2.0×] the base threshold.
- **Cointegration Gate**: Before executing any trade, the agent verifies the BTC/ETH pair is statistically cointegrated (Engle-Granger p < 0.05) over the rolling window. Trading is paused if the pair fails this check, since mean reversion is only valid on cointegrated pairs.
- **Real-Time Data**: Fetches live spot prices for BTC/USD and ETH/USD directly from the Coinbase API.
- **Safety Guardrails**:
  - **DRY_RUN Mode**: Toggle actual API trading on/off via environment variables.
  - **Position Sizing**: Trade size scales linearly with Z-score deviation, from a base percentage up to a configurable cap.
  - **Daily Stop Loss**: Halts the agent if the wallet's total value drops by more than **5%** within a UTC calendar day.

## Technical Stack
- **Language**: Python 3.11+
- **Key Libraries**: `coinbase-agentkit`, `pandas`, `statsmodels`, `requests`, `python-dotenv`
- **Infrastructure**: Docker & Docker Compose (Optimized for Unraid/Server deployment)

## Installation

### 1. Clone the Repository
```bash
git clone https://github.com/xbwomp/sentinel-alpha-p.git
cd sentinel-alpha-p
```

### 2. Configure Environment Variables
Copy the template and fill in your Coinbase CDP credentials:
```bash
cp .env.example .env
```

Edit the `.env` file with your specific values:
- `CDP_API_KEY_ID`: Your full CDP API Key Name (e.g., `organizations/.../apiKeys/...`)
- `CDP_API_KEY_SECRET`: Your PEM-formatted Private Key.
- `CDP_WALLET_SECRET`: Your generated Server Wallet Secret.
- `NETWORK_ID`: Set to `base-sepolia` for testing.

### 3. Docker Deployment
Build and start the container in detached mode:
```bash
docker-compose up -d --build
```

## Usage

### Monitoring Logs
The agent logs all signals, price updates, and trade executions to a local file that persists outside the Docker container.
```bash
tail -f trading_log.txt
```

### Strategy Parameters
You can fine-tune the agent's behavior by modifying the variables in the `.env` file:
- `Z_SCORE_THRESHOLD`: Base sensitivity of the mean reversion signals (Default: 2.0). Scaled adaptively at runtime.
- `WINDOW_SIZE_HOURS`: The lookback period for calculating the rolling mean (Default: 24).
- `TRADE_SIZE_PCT`: Base trade size as a fraction of balance at the threshold (Default: 0.10).
- `TRADE_SIZE_MAX_PCT`: Maximum trade size at extreme Z-scores (Default: 0.40).
- `ADAPTIVE_THRESHOLD_WINDOW`: Number of recent price points used for the vol-regime comparison (Default: 12, ~1 hour).
- `COINT_MIN_POINTS`: Minimum price points required before the cointegration check is run (Default: 20).
- `DRY_RUN`: Set to `false` to enable live trading on the Base network.

### Backtesting
Replay `price_history.json` through the strategy logic offline before going live:
```bash
python backtest.py                          # default params
python backtest.py --window 12 --threshold 1.8
python backtest.py --adaptive --coint-gate  # enable Tier 3 features
python backtest.py --help                   # full option list
```

### Unraid Deployment
- Map the project folder to an Unraid share.
- Use the Docker Compose Manager plugin to launch the service.
- Ensure the `trading_log.txt` and `wallet_data.json` are mapped as volumes to persist data across container updates.

## Future Enhancements

### Observability / Alerting
- **Webhook/Telegram alerts** on critical events (stop-loss trigger, trade execution, agent crash) via a `_send_alert()` helper posting to Discord/Slack/Telegram.
- **Track actual swap output** — parse the AgentKit `swap` result for real received amounts to enable accurate slippage and fee accounting vs. estimated P&L.
- **Dockerfile health check** — add a `HEALTHCHECK` that confirms `trading_log.txt` was updated within the last N minutes to detect silent agent crashes.

### Portfolio / Risk
- **Full portfolio stop-loss** — use `_get_portfolio_eth_value()` (ETH + cbBTC converted to ETH) for the daily stop-loss comparison instead of raw ETH balance only.
- **Hard position limit** — add a max cbBTC cap as a % of total portfolio to prevent over-concentration from multiple consecutive BUY signals.
- **Dollar-cost-average mode** — split large signals into N smaller trades over M minutes to reduce slippage, controlled by `DCA_SPLITS` and `DCA_INTERVAL_SECONDS` env vars.

### Dashboard / UX
- **Live Z-score sparkline** — plot the last 24h of Z-scores as a time series using Chart.js (data already available from `trades.json`).
- **Trade history table** — show last N trades with signal, Z-score, price, amount, and P&L (data already in `trades.json`).
- **Mobile-friendly layout** — refactor the current 3/4-column grid to collapse gracefully on small screens.

## Contributing
Contributions are welcome! Please follow these steps:
1. Fork the Project.
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`).
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`).
4. Push to the Branch (`git push origin feature/AmazingFeature`).
5. Open a Pull Request.

## License
Distributed under the MIT License. See `LICENSE` for more information.
