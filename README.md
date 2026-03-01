# Sentinel-Alpha Trading Agent

Sentinel-Alpha is an autonomous Mean Reversion trading agent built for the **Coinbase Base Network** (Base Sepolia Testnet). It utilizes the **Coinbase AgentKit SDK** to monitor BTC/ETH price ratios and execute automated "Shadow Trades" based on statistical deviations.

## Description

The agent implements a quantitative **Mean Reversion strategy** by calculating a rolling **Z-Score** of the BTC/ETH price ratio over a 24-hour window. 

### Core Logic:
- **Signal Generation**: 
  - **BUY**: Triggered when the Z-Score falls below **-2.0** (BTC is undervalued relative to ETH).
  - **SELL**: Triggered when the Z-Score rises above **2.0** (BTC is overvalued relative to ETH).
- **Real-Time Data**: Fetches live spot prices for BTC/USD and ETH/USD directly from the Coinbase API.
- **Safety Guardrails**:
  - **DRY_RUN Mode**: Toggle actual API trading on/off via environment variables.
  - **Position Sizing**: Automatically limits each trade to **2%** of the total wallet balance.
  - **Daily Stop Loss**: Halts the agent if the wallet's total value drops by more than **5%** within a 24-hour period.

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
- `Z_SCORE_THRESHOLD`: Sensitivity of the mean reversion signals (Default: 2.0).
- `WINDOW_SIZE_HOURS`: The lookback period for calculating the rolling mean (Default: 24).
- `DRY_RUN`: Set to `false` to enable live trading on the Base network.

### Unraid Deployment
- Map the project folder to an Unraid share.
- Use the Docker Compose Manager plugin to launch the service.
- Ensure the `trading_log.txt` and `wallet_data.json` are mapped as volumes to persist data across container updates.

## Contributing
Contributions are welcome! Please follow these steps:
1. Fork the Project.
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`).
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`).
4. Push to the Branch (`git push origin feature/AmazingFeature`).
5. Open a Pull Request.

## License
Distributed under the MIT License. See `LICENSE` for more information.
