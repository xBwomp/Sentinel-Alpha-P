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
from coinbase_agentkit import (
    AgentKit,
    AgentKitConfig,
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
TRADE_SIZE_PCT = float(os.getenv("TRADE_SIZE_PCT", 0.02))
DAILY_STOP_LOSS_PCT = float(os.getenv("DAILY_STOP_LOSS_PCT", 0.05))
NETWORK_ID = os.getenv("NETWORK_ID", "base-sepolia")

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("SentinelAlpha")

class SentinelAlpha:
    def __init__(self):
        self.wallet_provider = self._init_wallet()
        # Initialize AgentKit with the provider
        agent_kit_config = AgentKitConfig(wallet_provider=self.wallet_provider)
        self.agent_kit = AgentKit(agent_kit_config)
        
        # Initialize price history with dtypes
        self.price_history = pd.DataFrame(columns=["timestamp", "btc_price", "eth_price"])
        self.price_history["timestamp"] = pd.to_datetime(self.price_history["timestamp"])
        self.price_history["btc_price"] = self.price_history["btc_price"].astype(float)
        self.price_history["eth_price"] = self.price_history["eth_price"].astype(float)

        self.initial_daily_balance = None
        self.last_balance_check = datetime.min
        
    def _init_wallet(self):
        """Initialize the CDP Wallet Provider."""
        api_key_name = os.getenv("CDP_API_KEY_ID")
        api_key_private_key = os.getenv("CDP_API_KEY_SECRET", "").replace('\\n', '\n')
        wallet_secret = os.getenv("CDP_WALLET_SECRET")

        # Set environment variables that the provider looks for directly
        os.environ["CDP_API_KEY_NAME"] = api_key_name
        os.environ["CDP_API_KEY_PRIVATE_KEY"] = api_key_private_key
        if wallet_secret:
            os.environ["CDP_WALLET_SECRET"] = wallet_secret

        config = CdpEvmWalletProviderConfig(
            network_id=NETWORK_ID
        )
        
        provider = CdpEvmWalletProvider(config)
        logger.info(f"Wallet initialized on {NETWORK_ID}. Address: {provider.get_address()}")
        return provider

    def fetch_prices(self):
        """Fetch real BTC/USD and ETH/USD prices using the Coinbase API."""
        try:
            # We use the underlying CDP SDK client for public price data
            client = self.wallet_provider.get_client()
            
            # Note: The CDP SDK allows fetching prices for various pairs
            # Here we use the standard approach to get the spot price
            import requests
            
            # Using public Coinbase API for reliability in the fetch loop
            # This ensures we always get high-signal data for the Z-score
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
            
            # Maintain 24 hour window
            cutoff = datetime.now() - timedelta(hours=WINDOW_SIZE_HOURS)
            self.price_history = self.price_history[self.price_history["timestamp"] > cutoff]
            
            logger.info(f"Price Update - BTC: ${btc_price:,.2f} | ETH: ${eth_price:,.2f}")
            
        except Exception as e:
            logger.error(f"Error fetching real prices: {e}")

    def calculate_z_score(self):
        """Calculate the Z-Score of the BTC/ETH ratio."""
        if len(self.price_history) < 2: # Min 2 points for initial calc
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

    def check_stop_loss(self):
        """Verify if the daily stop loss has been triggered."""
        try:
            current_balance = float(self.wallet_provider.get_balance())
        except Exception as e:
            logger.error(f"Error checking balance: {e}")
            return True
        
        now = datetime.now()
        if self.initial_daily_balance is None or (now - self.last_balance_check).days >= 1:
            self.initial_daily_balance = current_balance
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

    def execute_trade(self, signal, asset="ETH"):
        """Execute a trade (or log a shadow trade in DRY_RUN)."""
        try:
            balance = float(self.wallet_provider.get_balance())
            trade_amount = balance * TRADE_SIZE_PCT
            
            if signal == "BUY":
                logger.info(f"SIGNAL: BUY (Z-Score too low). Shadow Trade: {trade_amount:.6f} {asset} (DRY_RUN={DRY_RUN})")
            elif signal == "SELL":
                logger.info(f"SIGNAL: SELL (Z-Score too high). Shadow Trade: {trade_amount:.6f} {asset} (DRY_RUN={DRY_RUN})")
                
            if not DRY_RUN:
                # Actual trading logic would go here
                # e.g., self.agent_kit.run("swap", {"from": "USD", "to": "ETH", "amount": trade_amount})
                pass
        except Exception as e:
            logger.error(f"Error in execution: {e}")

    def run(self):
        """Main loop."""
        logger.info(f"Sentinel-Alpha Started. Strategy: Mean Reversion | DRY_RUN: {DRY_RUN}")
        while True:
            try:
                if not self.check_stop_loss():
                    break
                    
                self.fetch_prices()
                z_score = self.calculate_z_score()
                
                if z_score is not None:
                    logger.info(f"Current Z-Score: {z_score:.4f} (Points: {len(self.price_history)})")
                    
                    if z_score < -Z_SCORE_THRESHOLD:
                        self.execute_trade("BUY")
                    elif z_score > Z_SCORE_THRESHOLD:
                        self.execute_trade("SELL")
                else:
                    logger.info("Collecting initial data points...")
                
                # Check every 5 minutes (300 seconds)
                time.sleep(300)
                
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                time.sleep(60)

if __name__ == "__main__":
    agent = SentinelAlpha()
    agent.run()
