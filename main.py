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
        
        # Initialize price history with dtypes to avoid warnings
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

        # Note: The current SDK version's CdpEvmWalletProvider initializes from environment variables.
        # We'll use a clean config if needed, but let it pick up from os.environ.
        config = CdpEvmWalletProviderConfig(
            network_id=NETWORK_ID
        )
        
        provider = CdpEvmWalletProvider(config)
        
        # Note: In this SDK version, wallet state is managed via the CDP Portal / Wallet Secret.
        # Exporting/importing local JSON is not required for CdpEvmWalletProvider.
            
        logger.info(f"Wallet initialized on {NETWORK_ID}. Address: {provider.get_address()}")
        return provider

    def fetch_prices(self):
        """Fetch current BTC and ETH prices."""
        try:
            # Placeholder for price discovery logic
            # In production, use a real price tool or API
            btc_price = 60000.0 + (np.random.rand() * 100)
            eth_price = 2500.0 + (np.random.rand() * 10)
            
            new_row = pd.DataFrame([{
                "timestamp": datetime.now(),
                "btc_price": btc_price,
                "eth_price": eth_price
            }])
            self.price_history = pd.concat([self.price_history, new_row], ignore_index=True)
            
            cutoff = datetime.now() - timedelta(hours=WINDOW_SIZE_HOURS + 1)
            self.price_history = self.price_history[self.price_history["timestamp"] > cutoff]
            
        except Exception as e:
            logger.error(f"Error fetching prices: {e}")

    def calculate_z_score(self):
        """Calculate the Z-Score of the BTC/ETH ratio."""
        if len(self.price_history) < 5:
            return None
        
        df = self.price_history.copy()
        df['ratio'] = df['btc_price'] / df['eth_price']
        
        rolling_mean = df['ratio'].mean()
        rolling_std = df['ratio'].std()
        
        if rolling_std == 0 or np.isnan(rolling_std):
            return 0
            
        current_ratio = df['ratio'].iloc[-1]
        z_score = (current_ratio - rolling_mean) / rolling_std
        return z_score

    def check_stop_loss(self):
        """Verify if the daily stop loss has been triggered."""
        try:
            # CdpEvmWalletProvider.get_balance() returns native balance (ETH)
            current_balance = float(self.wallet_provider.get_balance())
        except Exception as e:
            logger.error(f"Error checking balance: {e}")
            return True # Don't halt on transient balance error
        
        now = datetime.now()
        if self.initial_daily_balance is None or (now - self.last_balance_check).days >= 1:
            self.initial_daily_balance = current_balance
            self.last_balance_check = now
            logger.info(f"Daily balance tracking started: {current_balance} ETH")
            return True

        if self.initial_daily_balance == 0:
            return True

        drop = (self.initial_daily_balance - current_balance) / self.initial_daily_balance
        if drop > DAILY_STOP_LOSS_PCT:
            logger.critical(f"STOP LOSS TRIGGERED ({drop:.2%}). Agent shutting down.")
            return False
        return True

    def execute_trade(self, signal, asset="ETH"):
        """Execute a trade based on the signal."""
        try:
            # For EVM, we check the native balance for trades
            balance = float(self.wallet_provider.get_balance())
            trade_amount = balance * TRADE_SIZE_PCT
            
            if signal == "BUY":
                logger.info(f"SIGNAL: BUY {trade_amount} {asset} (DRY_RUN={DRY_RUN})")
                if not DRY_RUN:
                    # In a real scenario, use the agent_kit to execute an action
                    # e.g., self.agent_kit.run("swap", ...)
                    pass
            elif signal == "SELL":
                logger.info(f"SIGNAL: SELL {trade_amount} {asset} (DRY_RUN={DRY_RUN})")
                if not DRY_RUN:
                    # Logic for actual trade
                    pass
        except Exception as e:
            logger.error(f"Error executing trade: {e}")

    def run(self):
        """Main agent loop."""
        logger.info("Sentinel-Alpha Agent Started.")
        while True:
            try:
                if not self.check_stop_loss():
                    break
                    
                self.fetch_prices()
                z_score = self.calculate_z_score()
                
                if z_score is not None:
                    logger.info(f"Current Z-Score: {z_score:.4f}")
                    
                    if z_score < -Z_SCORE_THRESHOLD:
                        self.execute_trade("BUY")
                    elif z_score > Z_SCORE_THRESHOLD:
                        self.execute_trade("SELL")
                else:
                    logger.info("Collecting price data...")
                
                # In actual operation, this would be longer (e.g. 300s)
                # Reduced for initial verification loop
                time.sleep(300)
                
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                time.sleep(60)

if __name__ == "__main__":
    agent = SentinelAlpha()
    agent.run()
