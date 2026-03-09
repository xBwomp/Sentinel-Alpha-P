import os
import time
import json
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from decimal import Decimal
from dotenv import load_dotenv
from statsmodels.tsa.stattools import coint
import asyncio
from web3 import Web3

from coinbase_agentkit import (
    AgentKit,
    AgentKitConfig,
)
from coinbase_agentkit.action_providers import (
    cdp_evm_wallet_action_provider,
    erc20_action_provider,
)
from coinbase_agentkit.wallet_providers import (
    CdpEvmWalletProvider,
    CdpEvmWalletProviderConfig
)

# Load environment variables
load_dotenv()

# Configuration
LOG_FILE = os.getenv("LOG_FILE", "trading_log.txt")
WALLET_DATA_FILE = os.getenv("WALLET_DATA_FILE", "wallet_data.json")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
Z_SCORE_THRESHOLD = float(os.getenv("Z_SCORE_THRESHOLD", 2.0))
WINDOW_SIZE_HOURS = int(os.getenv("WINDOW_SIZE_HOURS", 24))
TRADE_SIZE_PCT = float(os.getenv("TRADE_SIZE_PCT", 0.10))      # base size at threshold
TRADE_SIZE_MAX_PCT = float(os.getenv("TRADE_SIZE_MAX_PCT", 0.40)) # cap at extreme Z-scores
DAILY_STOP_LOSS_PCT = float(os.getenv("DAILY_STOP_LOSS_PCT", 0.05))
NETWORK_ID = os.getenv("NETWORK_ID", "base-sepolia")
TRADES_FILE = os.getenv("TRADES_FILE", "trades.json")
DAILY_SUMMARY_FILE = os.getenv("DAILY_SUMMARY_FILE", "daily_summary.json")

# Guardrails
COOLDOWN_PERIOD = timedelta(hours=1) # Don't trade more than once per hour
MIN_TRADE_ETH = 0.0001 # Minimum trade size to avoid dust (~$0.20 at $2k ETH)
MIN_TRADE_CBBTC = 0.0001 # Minimum cbBTC sell size to avoid dust (~$7 at $70k BTC)
COINT_MIN_POINTS = int(os.getenv("COINT_MIN_POINTS", 20))       # min price points before cointegration check
COINT_P_THRESHOLD = float(os.getenv("COINT_P_THRESHOLD", 0.25)) # max p-value to allow trading (0.05=strict, 0.25=loose)
ADAPTIVE_THRESHOLD_WINDOW = int(os.getenv("ADAPTIVE_THRESHOLD_WINDOW", 12))  # recent-vol window (points)

# Constants
NATIVE_ETH = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
TOKENS = {
    "base-mainnet": {
        "cbBTC": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        "WETH": "0x4200000000000000000000000000000000000006"
    },
    "base-sepolia": {
        "cbBTC": "0xcbB7C0006F23900c38EB856149F799620fcb8A4a",
        "WETH": "0x4200000000000000000000000000000000000006"
    }
}

# Setup Logging
logger = logging.getLogger("SentinelAlpha")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.propagate = False

class SentinelAlpha:
    def __init__(self):
        self.wallet_provider = self._init_wallet()
        # Initialize AgentKit with the provider and specific action providers
        agent_kit_config = AgentKitConfig(
            wallet_provider=self.wallet_provider,
            action_providers=[
                cdp_evm_wallet_action_provider(),
                erc20_action_provider(),
            ]
        )
        self.agent_kit = AgentKit(agent_kit_config)
        
        # Cache available actions
        self.actions = {action.name: action for action in self.agent_kit.get_actions()}
        
        # Initialize price history with dtypes
        self.price_history = pd.DataFrame(columns=["timestamp", "btc_price", "eth_price"])
        self.price_history["timestamp"] = pd.to_datetime(self.price_history["timestamp"])
        self.price_history["btc_price"] = self.price_history["btc_price"].astype(float)
        self.price_history["eth_price"] = self.price_history["eth_price"].astype(float)
        self._load_price_history()

        self.initial_daily_balance = None
        self.daily_reset_date = None
        self.last_balance_check = datetime.min
        self.last_trade_time = datetime.min
        self.trades_in_last_24h = []
        self._seed_trade_state()

        # Cointegration gate state
        self._coint_p_value = None
        self._coint_last_size = 0

        # Daily metrics tracking
        self.z_scores_today = []
        self.trades_executed_today = 0
        self.signals_ignored_cooldown_today = 0
        self.signals_ignored_limit_today = 0
        self.last_summary_date = datetime.now().date()
        
    def _init_wallet(self):
        """Initialize the CDP Wallet Provider with persistence using address and idempotency key."""
        api_key_name = os.getenv("CDP_API_KEY_ID")
        api_key_private_key = os.getenv("CDP_API_KEY_SECRET", "").replace('\\n', '\n')
        wallet_secret = os.getenv("CDP_WALLET_SECRET")
        
        # Set environment variables for the SDK
        os.environ["CDP_API_KEY_ID"] = api_key_name
        os.environ["CDP_API_KEY_SECRET"] = api_key_private_key
        os.environ["CDP_WALLET_SECRET"] = wallet_secret

        wallet_address = None
        if os.path.exists(WALLET_DATA_FILE):
            try:
                with open(WALLET_DATA_FILE, 'r') as f:
                    data = json.load(f)
                    wallet_address = data.get("address")
                if wallet_address:
                    logger.info(f"Loading existing wallet address from {WALLET_DATA_FILE}: {wallet_address}")
            except Exception as e:
                logger.error(f"Could not load wallet data: {e}")

        # Use a fixed idempotency key to ensure we don't create multiple wallets
        # even if the address file is lost (CDP will return the same wallet for the same key)
        config = CdpEvmWalletProviderConfig(
            network_id=NETWORK_ID,
            address=wallet_address,
            idempotency_key="sentinel_alpha_main_wallet"
        )
        
        provider = CdpEvmWalletProvider(config)
        
        # Save the current address for next time
        try:
            current_address = provider.get_address()
            with open(WALLET_DATA_FILE, 'w') as f:
                json.dump({"address": current_address}, f)
            logger.info(f"Wallet address {current_address} persisted to {WALLET_DATA_FILE}")
        except Exception as e:
            logger.error(f"Failed to persist wallet address: {e}")

        logger.info(f"Wallet initialized on {NETWORK_ID}. Address: {provider.get_address()}")
        return provider

    def _get_action(self, keyword):
        """Helper to find an action by keyword (suffix match)."""
        for name, action in self.actions.items():
            if name.endswith(keyword):
                return action
        return None

    def _get_portfolio_eth_value(self, btc_price, eth_price):
        """Return total portfolio value in ETH-equivalent (ETH + cbBTC converted to ETH)."""
        eth_balance = self.get_token_balance(NATIVE_ETH)
        cbbtc_address = TOKENS[NETWORK_ID]["cbBTC"]
        cbbtc_balance = self.get_token_balance(cbbtc_address)
        cbbtc_in_eth = (cbbtc_balance * btc_price / eth_price) if eth_price > 0 else 0
        return eth_balance + cbbtc_in_eth

    def _load_price_history(self):
        """Seed price_history from disk on startup, discarding points outside the rolling window."""
        path = "price_history.json"
        if os.path.exists(path):
            try:
                df = pd.read_json(path)
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                cutoff = datetime.now() - timedelta(hours=WINDOW_SIZE_HOURS)
                self.price_history = df[df["timestamp"] > cutoff].reset_index(drop=True)
                logger.info(f"Loaded {len(self.price_history)} historical price points from disk.")
            except Exception as e:
                logger.warning(f"Could not load price history: {e}")

    def _save_price_history(self):
        """Persist the current price_history window to disk."""
        try:
            self.price_history.to_json("price_history.json", orient="records", date_format="iso")
        except Exception as e:
            logger.warning(f"Could not save price history: {e}")

    def _log_trade_structured(self, signal, z_score, trade_amount,
                               actual_from_amount=None, actual_to_amount=None):
        """Append a structured trade record to trades.json (JSONL format)."""
        if len(self.price_history) == 0:
            return
        latest = self.price_history.iloc[-1]
        btc_price = float(latest['btc_price'])
        eth_price = float(latest['eth_price'])
        ratio = btc_price / eth_price if eth_price > 0 else 0
        portfolio_eth_value = self._get_portfolio_eth_value(btc_price, eth_price)

        # Compute slippage: how much the actual received amount differed from the naive estimate.
        # Positive = received more than expected (good); negative = slippage cost (bad).
        slippage_pct = None
        if actual_to_amount is not None and eth_price > 0 and btc_price > 0:
            if signal == "BUY":
                expected_to = trade_amount * eth_price / btc_price
            else:
                expected_to = trade_amount * btc_price / eth_price
            if expected_to > 0:
                slippage_pct = round((actual_to_amount - expected_to) / expected_to * 100, 4)

        entry = {
            "timestamp": datetime.now().isoformat(),
            "signal": signal,
            "z_score": round(z_score, 6),
            "btc_price": btc_price,
            "eth_price": eth_price,
            "ratio": round(ratio, 6),
            "trade_amount": trade_amount,
            "actual_from_amount": actual_from_amount,
            "actual_to_amount": actual_to_amount,
            "slippage_pct": slippage_pct,
            "portfolio_eth_value": round(portfolio_eth_value, 8),
            "dry_run": DRY_RUN,
        }
        try:
            with open(TRADES_FILE, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error(f"Failed to write structured trade log: {e}")

    def _write_daily_summary(self):
        """Write end-of-day summary to daily_summary.json and reset daily counters."""
        if not self.z_scores_today or len(self.price_history) == 0:
            return
        latest = self.price_history.iloc[-1]
        btc_price = float(latest['btc_price'])
        eth_price = float(latest['eth_price'])
        portfolio_eth_value = self._get_portfolio_eth_value(btc_price, eth_price)

        stop_loss_proximity_pct = None
        if self.initial_daily_balance and self.initial_daily_balance > 0:
            stop_loss_floor = self.initial_daily_balance * (1 - DAILY_STOP_LOSS_PCT)
            stop_loss_proximity_pct = round(
                (portfolio_eth_value - stop_loss_floor) / self.initial_daily_balance * 100, 2
            )

        summary = {
            "date": self.last_summary_date.isoformat(),
            "trades_executed": self.trades_executed_today,
            "signals_ignored_cooldown": self.signals_ignored_cooldown_today,
            "signals_ignored_limit": self.signals_ignored_limit_today,
            "z_score_min": round(min(self.z_scores_today), 4),
            "z_score_max": round(max(self.z_scores_today), 4),
            "z_score_mean": round(sum(self.z_scores_today) / len(self.z_scores_today), 4),
            "portfolio_eth_value_eod": round(portfolio_eth_value, 8),
            "stop_loss_proximity_pct": stop_loss_proximity_pct,
            "btc_price_eod": btc_price,
            "eth_price_eod": eth_price,
        }
        try:
            with open(DAILY_SUMMARY_FILE, "a") as f:
                f.write(json.dumps(summary) + "\n")
            logger.info(
                f"Daily summary written for {self.last_summary_date}: "
                f"{self.trades_executed_today} trades, "
                f"portfolio={portfolio_eth_value:.6f} ETH-equiv"
            )
        except Exception as e:
            logger.error(f"Failed to write daily summary: {e}")

        # Reset daily counters
        self.z_scores_today = []
        self.trades_executed_today = 0
        self.signals_ignored_cooldown_today = 0
        self.signals_ignored_limit_today = 0

    def get_token_balance(self, token_address):
        """Get balance for native ETH or ERC20 token using AgentKit tools."""
        try:
            if token_address == NATIVE_ETH:
                return float(self.wallet_provider.get_balance()) / 1e18
            else:
                balance_tool = self._get_action("erc20_get_balance")
                if not balance_tool:
                    from coinbase_agentkit.action_providers.erc20.constants import ERC20_ABI
                    balance_wei = self.wallet_provider.read_contract(
                        contract_address=token_address,
                        abi=ERC20_ABI,
                        function_name="balanceOf",
                        args=[self.wallet_provider.get_address()]
                    )
                    decimals = self.wallet_provider.read_contract(
                        contract_address=token_address,
                        abi=ERC20_ABI,
                        function_name="decimals"
                    )
                    return float(balance_wei) / (10**decimals)
                
                result = balance_tool.invoke({"contract_address": token_address})
                import re
                match = re.search(r"is ([\d\.]+)", result)
                if match:
                    return float(match.group(1))
                return 0.0
        except Exception as e:
            logger.error(f"Error getting balance for {token_address}: {e}")
            return 0.0

    def fetch_prices(self):
        """Fetch real BTC/USD and ETH/USD prices using the Coinbase API."""
        try:
            import requests
            btc_data = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot").json()
            eth_data = requests.get("https://api.coinbase.com/v2/prices/ETH-USD/spot").json()
            
            btc_price = float(btc_data['data']['amount'])
            eth_price = float(eth_data['data']['amount'])
            
            new_row = pd.DataFrame([{
                "timestamp": datetime.now(),
                "btc_price": btc_price,
                "eth_price": eth_price
            }])
            self.price_history = pd.concat([self.price_history, new_row], ignore_index=True)
            
            cutoff = datetime.now() - timedelta(hours=WINDOW_SIZE_HOURS)
            self.price_history = self.price_history[self.price_history["timestamp"] > cutoff]
            
            logger.info(f"Price Update - BTC: ${btc_price:,.2f} | ETH: ${eth_price:,.2f}")
            self._save_price_history()

        except Exception as e:
            logger.error(f"Error fetching real prices: {e}")

    def calculate_z_score(self):
        """Calculate the Z-Score of the BTC/ETH ratio."""
        if len(self.price_history) < 2:
            return None
        
        df = self.price_history.copy()
        df['ratio'] = df['btc_price'] / df['eth_price']
        
        rolling_mean = df['ratio'].mean()
        rolling_std = df['ratio'].std()
        
        if rolling_std == 0 or np.isnan(rolling_std) or len(df) < 2:
            return 0
            
        current_ratio = df['ratio'].iloc[-1]
        z_score = (current_ratio - rolling_mean) / rolling_std
        return z_score

    def _check_cointegration(self):
        """Run Engle-Granger cointegration test on the rolling price window.

        Returns (is_valid, p_value). Re-runs every 12 new price points (~1 hour).
        Fails open on errors so a numerical failure never permanently blocks trading.
        """
        n = len(self.price_history)
        if n < COINT_MIN_POINTS:
            return False, None
        if self._coint_p_value is not None and n - self._coint_last_size < 12:
            return self._coint_p_value < COINT_P_THRESHOLD, self._coint_p_value
        try:
            btc = self.price_history["btc_price"].values
            eth = self.price_history["eth_price"].values
            _, p_value, _ = coint(btc, eth)
            self._coint_p_value = p_value
            self._coint_last_size = n
            valid = p_value < COINT_P_THRESHOLD
            logger.info(
                f"Cointegration check: p={p_value:.4f} "
                f"({'VALID' if valid else 'INVALID — trading paused'})"
            )
            return valid, p_value
        except Exception as e:
            logger.warning(f"Cointegration test failed: {e}. Failing open.")
            return True, None

    def _adaptive_threshold(self):
        """Return Z-score threshold scaled by current volatility regime.

        Compares recent ratio vol (last ADAPTIVE_THRESHOLD_WINDOW points) to the
        full-window vol. Raises the threshold in high-vol regimes (noise reduction)
        and lowers it in low-vol regimes (higher sensitivity). Clamped to [0.5x, 2.0x].
        """
        if len(self.price_history) < ADAPTIVE_THRESHOLD_WINDOW + 2:
            return Z_SCORE_THRESHOLD
        ratio = self.price_history["btc_price"] / self.price_history["eth_price"]
        full_std = ratio.std()
        recent_std = ratio.iloc[-ADAPTIVE_THRESHOLD_WINDOW:].std()
        if full_std == 0 or np.isnan(full_std) or np.isnan(recent_std):
            return Z_SCORE_THRESHOLD
        scale = max(0.5, min(2.0, recent_std / full_std))
        return Z_SCORE_THRESHOLD * scale

    def _seed_trade_state(self):
        """Seed cooldown state from trades.json on startup to survive restarts."""
        if not os.path.exists(TRADES_FILE):
            return
        try:
            cutoff = datetime.now() - timedelta(days=1)
            with open(TRADES_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        ts = datetime.fromisoformat(record["timestamp"])
                        if ts > cutoff:
                            self.trades_in_last_24h.append(ts)
                        if ts > self.last_trade_time:
                            self.last_trade_time = ts
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
            if self.trades_in_last_24h:
                logger.info(
                    f"Seeded {len(self.trades_in_last_24h)} recent trades from {TRADES_FILE}. "
                    f"Last trade: {self.last_trade_time}"
                )
        except Exception as e:
            logger.warning(f"Could not seed trade state from {TRADES_FILE}: {e}")

    def check_stop_loss(self):
        """Verify if the daily stop loss has been triggered."""
        current_balance = self.get_token_balance(NATIVE_ETH)

        today_utc = datetime.utcnow().date()
        now = datetime.now()
        if self.initial_daily_balance is None or self.daily_reset_date != today_utc:
            self.initial_daily_balance = current_balance
            self.daily_reset_date = today_utc
            self.last_balance_check = now
            logger.info(f"Daily balance reset: {current_balance} ETH")
            return True

        if self.initial_daily_balance == 0:
            return True

        drop = (self.initial_daily_balance - current_balance) / self.initial_daily_balance
        if drop > DAILY_STOP_LOSS_PCT:
            logger.critical(f"STOP LOSS TRIGGERED ({drop:.2%}). Halting script.")
            return False
        return True

    def _scaled_trade_pct(self, z_score: float) -> float:
        """Scale trade size linearly with Z-score deviation beyond the threshold.

        At Z = threshold  -> TRADE_SIZE_PCT (base)
        At Z = threshold*2 -> TRADE_SIZE_MAX_PCT (cap)
        Clamped so it never exceeds TRADE_SIZE_MAX_PCT.
        """
        excess = abs(z_score) - Z_SCORE_THRESHOLD          # how far past the trigger
        scale = excess / Z_SCORE_THRESHOLD                  # 0.0 at threshold, 1.0 at 2x threshold
        scale = max(0.0, min(scale, 1.0))                   # clamp 0–1
        pct = TRADE_SIZE_PCT + scale * (TRADE_SIZE_MAX_PCT - TRADE_SIZE_PCT)
        return pct

    def execute_trade(self, signal, z_score=0.0):
        """Execute a trade (or log a shadow trade in DRY_RUN)."""
        now = datetime.now()

        # Clean up old trade timestamps (older than 24h)
        self.trades_in_last_24h = [t for t in self.trades_in_last_24h if now - t < timedelta(days=1)]

        if len(self.trades_in_last_24h) >= 50:
            logger.warning(f"Daily trade limit (50) reached. Ignoring {signal} signal to save operations.")
            self.signals_ignored_limit_today += 1
            return

        if now - self.last_trade_time < COOLDOWN_PERIOD:
            logger.info(f"Signal {signal} ignored due to cooldown.")
            self.signals_ignored_cooldown_today += 1
            return

        try:
            cbbtc_address = TOKENS[NETWORK_ID]["cbBTC"]
            
            if signal == "BUY":
                # Buy BTC (Swap ETH -> cbBTC)
                from_token = NATIVE_ETH
                to_token = cbbtc_address
                balance = self.get_token_balance(from_token)
                scaled_pct = self._scaled_trade_pct(z_score)
                trade_amount = max(balance * scaled_pct, MIN_TRADE_ETH)

                if balance < trade_amount:
                    logger.warning(f"Insufficient ETH balance ({balance:.6f}) to trade. Skipping.")
                    return

                logger.info(f"SIGNAL: BUY BTC (ETH -> cbBTC). Amount: {trade_amount:.6f} ETH ({scaled_pct*100:.1f}% of balance, Z={z_score:.2f}, DRY_RUN={DRY_RUN})")
            
            elif signal == "SELL":
                # Sell BTC (Swap cbBTC -> ETH)
                from_token = cbbtc_address
                to_token = NATIVE_ETH
                balance = self.get_token_balance(from_token)
                scaled_pct = self._scaled_trade_pct(z_score)
                trade_amount = max(balance * scaled_pct, MIN_TRADE_CBBTC)

                if balance < trade_amount:
                    logger.warning(f"Insufficient cbBTC balance ({balance:.8f}) to trade. Skipping.")
                    return

                logger.info(f"SIGNAL: SELL BTC (cbBTC -> ETH). Amount: {trade_amount:.8f} cbBTC ({scaled_pct*100:.1f}% of balance, Z={z_score:.2f}, DRY_RUN={DRY_RUN})")

            if not DRY_RUN:
                swap_tool = self._get_action("swap")
                if not swap_tool:
                    logger.error(f"Swap tool not found. Available actions: {list(self.actions.keys())}")
                    return

                # Execute the swap
                result_str = swap_tool.invoke({
                    "from_token": from_token,
                    "to_token": to_token,
                    "from_amount": str(trade_amount)
                })
                try:
                    result = json.loads(result_str)
                except (json.JSONDecodeError, TypeError):
                    result = {"success": False, "error": f"Unparseable response: {result_str}"}

                if not result.get("success"):
                    logger.error(f"Swap failed: {result.get('error', 'unknown error')}")
                    return

                actual_from = None
                actual_to = None
                try:
                    actual_from = float(result.get("fromAmount") or 0) or None
                    actual_to = float(result.get("toAmount") or 0) or None
                except (TypeError, ValueError):
                    pass

                logger.info(
                    f"Swap executed: {actual_from} {result.get('fromTokenName')} -> "
                    f"{actual_to} {result.get('toTokenName')} | tx: {result.get('transactionHash')}"
                )
                self.last_trade_time = now
                self.trades_in_last_24h.append(now)
            else:
                actual_from = None
                actual_to = None
                self.last_trade_time = now
                self.trades_in_last_24h.append(now)

            self.trades_executed_today += 1
            self._log_trade_structured(signal, z_score, trade_amount, actual_from, actual_to)

        except Exception as e:
            logger.error(f"Error in execution: {e}")

    def run(self):
        """Main loop."""
        logger.info(f"Sentinel-Alpha Started. Strategy: Mean Reversion | DRY_RUN: {DRY_RUN}")
        while True:
            try:
                # Day rollover: write daily summary and reset counters
                today = datetime.now().date()
                if today != self.last_summary_date:
                    self._write_daily_summary()
                    self.last_summary_date = today

                if not self.check_stop_loss():
                    break

                self.fetch_prices()
                z_score = self.calculate_z_score()

                if z_score is not None:
                    self.z_scores_today.append(z_score)
                    threshold = self._adaptive_threshold()
                    coint_valid, coint_p = self._check_cointegration()
                    p_str = f"{coint_p:.4f}" if coint_p is not None else "N/A"
                    logger.info(
                        f"Current Z-Score: {z_score:.4f} "
                        f"(Points: {len(self.price_history)}, "
                        f"Threshold: {threshold:.2f}, Coint-p: {p_str})"
                    )

                    if not coint_valid:
                        logger.info("Cointegration gate: pair not cointegrated — no trades.")
                    elif z_score < -threshold:
                        self.execute_trade("BUY", z_score)
                    elif z_score > threshold:
                        self.execute_trade("SELL", z_score)
                else:
                    logger.info("Collecting initial data points...")

                time.sleep(300)

            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                time.sleep(60)

if __name__ == "__main__":
    agent = SentinelAlpha()
    agent.run()
