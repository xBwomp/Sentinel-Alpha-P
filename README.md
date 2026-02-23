# 🤖 Project: Sentinel-Alpha
**Autonomous Trading & Portfolio Rebalancing Agent**

## 1. Executive Summary
Sentinel-Alpha is a high-reliability trading agent designed for the **Base Network**. It utilizes a dual-strategy approach to extract value from market volatility while maintaining strict capital preservation guardrails.

## 2. Core Strategies (Phase 1)
* **Z-Score Mean Reversion:** Monitors real-world BTC/USD price feeds. Executes trades when the price deviates beyond $\pm2.0$ standard deviations from the 24-hour rolling mean.
* **Portfolio Rebalancing:** Maintains a strict **50/50 BTC/ETH** asset distribution. Automatically self-corrects if either asset drifts more than **3%** from its target weight.
* **Shadow Trading:** Currently configured to pull **Live Market Data** but execute trades on the **Base Sepolia Testnet** for zero-risk performance validation.

## 3. The Competitive Edge (Security & Execution)
* **MEV Protection:** Transactions are routed through a **Private RPC** (`https://rpc-sepolia.flashbots.net/`) to prevent predatory front-running and sandwich attacks.
* **Intent-Based Logic:** The agent signals "intent" to the network, ensuring trades are only executed under optimal slippage conditions.

## 4. System Requirements & Guardrails
* **Infrastructure:** Unraid Server (Dockerized).
* **Capital Protection:** * **2% Rule:** No single trade exceeds 2% of the total wallet value.
    * **Daily Stop-Loss:** Automatic "Kill-Switch" if the wallet value drops >5% in 24 hours.
* **Monitoring:** Requires the Unraid **User Scripts** plugin for log tailing.

## 5. Deployment Guide
1.  **Directory:** Create `/mnt/user/appdata/sentinel-alpha`.
2.  **Files:** Upload `main.py`, `Dockerfile`, `docker-compose.yml`, and `.env`.
3.  **Permissions:** In the Coinbase CDP Dashboard, ensure API keys have:
    * `Wallet`: view, create, transfer
    * `Trading`: execute_trade, view_price
    * `Network`: Base Sepolia enabled.
4.  **Launch:** Run `docker-compose up -d` in the Unraid terminal.

## 6. Troubleshooting & Support
| Issue | Common Cause | Resolution |
| :--- | :--- | :--- |
| **Container Crash** | Python library mismatch | Run `docker logs sentinel-alpha` to identify missing modules. |
| **Access Denied** | Key permissions | Verify 'Base Sepolia' is checked in your CDP API Key settings. |
| **No Price Data** | RPC Timeout | Ensure Unraid Firewall allows traffic to `flashbots.net`. |

## 7. Future Roadmap (Phase 2)
* **Dynamic Asset Discovery:** Automated scanning for top-volume tokens.
* **Scam Token Filtering:** Integration with Coinbase Scam Detection API.
* **Sentiment Analysis:** LLM-based news filtering to avoid "falling knife" trades.

You will need your agent's wallet address (from the Docker logs) to use these:

Alchemy Base Sepolia Faucet: The most reliable for Test-ETH (Gas fees).
https://www.alchemy.com/faucets/base-sepolia

Chainlink Faucet: Great for ETH and other test tokens.
https://faucets.chain.link/base-sepolia

LearnWeb3 USDC Faucet: Specifically for getting Test-USDC to test your trading pairs.
https://learnweb3.io/faucets/base_sepolia_usdc/

