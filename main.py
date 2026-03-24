import os
import time
import json
import logging
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List
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
TRADE_SIZE_PCT = float(os.getenv("TRADE_SIZE_PCT", 0.10))
TRADE_SIZE_MAX_PCT = float(os.getenv("TRADE_SIZE_MAX_PCT", 0.40))
DAILY_STOP_LOSS_PCT = float(os.getenv("DAILY_STOP_LOSS_PCT", 0.05))
NETWORK_ID = os.getenv("NETWORK_ID", "base-sepolia")
RPC_URL = os.getenv("RPC_URL", None)
SWAP_SLIPPAGE_BPS = int(os.getenv("SWAP_SLIPPAGE_BPS", 200))
TRADES_FILE = os.getenv("TRADES_FILE", "trades.json")
DAILY_SUMMARY_FILE = os.getenv("DAILY_SUMMARY_FILE", "daily_summary.json")
# Trade-size scaling: ramp reaches TRADE_SIZE_MAX_PCT at Z_SCORE_THRESHOLD + TRADE_SCALE_RAMP
# Default 1.0 → max size reached at Z=3.0 (was Z=4.0 when equal to threshold)
TRADE_SCALE_RAMP = float(os.getenv("TRADE_SCALE_RAMP", "1.0"))

# Telegram notifications (optional — leave blank to disable)
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Aave V3 yield on idle ETH (Base mainnet only; disabled by default)
# Verify addresses at https://app.aave.com/
AAVE_ENABLED = os.getenv("AAVE_ENABLED", "false").lower() == "true"
AAVE_POOL_ADDRESS  = os.getenv("AAVE_POOL_ADDRESS",  "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5")
AAVE_AWETH_ADDRESS = os.getenv("AAVE_AWETH_ADDRESS", "0xD4a0e0b9149BCee3C920d2E00b5dE09138fd8bb7")
AAVE_ETH_RESERVE   = float(os.getenv("AAVE_ETH_RESERVE",  "0.001"))  # ETH kept liquid for gas
AAVE_MIN_DEPOSIT   = float(os.getenv("AAVE_MIN_DEPOSIT",   "0.005"))  # minimum worth depositing

# Guardrails
COOLDOWN_PERIOD = timedelta(hours=1)
MIN_TRADE_ETH = 0.0001
COINT_MIN_POINTS = int(os.getenv("COINT_MIN_POINTS", 20))
COINT_P_THRESHOLD = float(os.getenv("COINT_P_THRESHOLD", 0.25))
ADAPTIVE_THRESHOLD_WINDOW = int(os.getenv("ADAPTIVE_THRESHOLD_WINDOW", 12))

# ABIs
WETH_ABI = [
    {"inputs": [],                                                                              "name": "deposit",   "outputs": [],                                   "stateMutability": "payable",    "type": "function"},
    {"inputs": [{"name": "wad",     "type": "uint256"}],                                        "name": "withdraw",  "outputs": [],                                   "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "owner",   "type": "address"}, {"name": "spender", "type": "address"}],"name": "allowance", "outputs": [{"name": "", "type": "uint256"}],    "stateMutability": "view",       "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount",  "type": "uint256"}],"name": "approve",   "outputs": [{"name": "", "type": "bool"}],        "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}],                                        "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],    "stateMutability": "view",       "type": "function"},
]
AAVE_POOL_ABI = [
    {"inputs": [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"}, {"name": "onBehalfOf", "type": "address"}, {"name": "referralCode", "type": "uint16"}],
     "name": "supply",   "outputs": [],                                "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"}, {"name": "to",         "type": "address"}],
     "name": "withdraw", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
]

# Constants
NATIVE_ETH = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
TOKENS = {
    "base-mainnet": {
        "cbBTC": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        "WETH":  "0x4200000000000000000000000000000000000006",
        "cbETH": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22",
    },
    "base-sepolia": {
        "cbBTC": "0xcbB7C0006F23900c38EB856149F799620fcb8A4a",
        "WETH":  "0x4200000000000000000000000000000000000006",
        "cbETH": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22",
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


# ── Trading Pair Definition ───────────────────────────────────────────────────

@dataclass
class TradingPair:
    """All state and config for a single mean-reversion pair."""
    name: str               # "BTC/ETH" — used in logs and trades.json
    base_symbol: str        # Coinbase API symbol for numerator, e.g. "BTC", "CBETH"
    quote_symbol: str       # Coinbase API symbol for denominator, always "ETH" for now
    base_token: str         # ERC-20 address of the base asset (what we buy/sell)
    quote_token: str        # ERC-20 address (or NATIVE_ETH) of the quote asset
    capital_fraction: float # fraction of shared ETH pool this pair may use (e.g. 0.5)
    min_base_amount: float  # minimum base-token sell size to avoid dust
    enabled_networks: List[str] = field(default_factory=list)  # empty = all networks

    # Per-pair price history (columns: timestamp, base_price, quote_price)
    price_history: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(
            columns=["timestamp", "base_price", "quote_price"]
        )
    )

    # Per-pair cooldown / daily-cap state
    last_trade_time: datetime = field(default_factory=lambda: datetime.min)
    trades_in_last_24h: List[datetime] = field(default_factory=list)

    # Per-pair cointegration cache
    _coint_p_value: Optional[float] = None
    _coint_last_size: int = 0

    # Per-pair daily metrics (reset at midnight)
    z_scores_today: List[float] = field(default_factory=list)
    trades_executed_today: int = 0
    signals_ignored_cooldown_today: int = 0
    signals_ignored_limit_today: int = 0

    def is_enabled(self) -> bool:
        return not self.enabled_networks or NETWORK_ID in self.enabled_networks

    def reset_daily_counters(self):
        self.z_scores_today = []
        self.trades_executed_today = 0
        self.signals_ignored_cooldown_today = 0
        self.signals_ignored_limit_today = 0


# ── Agent ─────────────────────────────────────────────────────────────────────

class SentinelAlpha:
    def __init__(self):
        self.wallet_provider = self._init_wallet()

        agent_kit_config = AgentKitConfig(
            wallet_provider=self.wallet_provider,
            action_providers=[
                cdp_evm_wallet_action_provider(),
                erc20_action_provider(),
            ]
        )
        self.agent_kit = AgentKit(agent_kit_config)
        self.actions = {action.name: action for action in self.agent_kit.get_actions()}

        # ── Define trading pairs ──────────────────────────────────────────────
        num_pairs = 2
        fraction = round(1.0 / num_pairs, 4)

        self.pairs: List[TradingPair] = [
            TradingPair(
                name="BTC/ETH",
                base_symbol="BTC",
                quote_symbol="ETH",
                base_token=TOKENS[NETWORK_ID]["cbBTC"],
                quote_token=NATIVE_ETH,
                capital_fraction=fraction,
                min_base_amount=0.0001,      # ~$7 at $70k BTC
                enabled_networks=[],          # all networks
            ),
            TradingPair(
                name="cbETH/ETH",
                base_symbol="CBETH",
                quote_symbol="ETH",
                base_token=TOKENS[NETWORK_ID]["cbETH"],
                quote_token=NATIVE_ETH,
                capital_fraction=fraction,
                min_base_amount=0.0001,      # ~$0.20 at $2k ETH
                enabled_networks=["base-mainnet"],  # no testnet liquidity
            ),
        ]

        # ── Portfolio-level state ─────────────────────────────────────────────
        self.initial_daily_balance: Optional[float] = None
        self.daily_reset_date: Optional[object] = None
        self.last_summary_date = datetime.now().date()
        self._balance_cache: dict = {}  # token_address -> (value, timestamp)

        # ── Network health / stop-loss guards ────────────────────────────────
        self._consecutive_price_failures: int = 0
        self._stale_balance_used: bool = False
        self._aave_balance_cache: Optional[float] = None  # last known good Aave balance
        self._stop_loss_first_seen: Optional[datetime] = None  # cooldown start

        self._load_price_history()
        self._seed_trade_state()

    # ── Wallet ────────────────────────────────────────────────────────────────

    def _init_wallet(self):
        api_key_name = os.getenv("CDP_API_KEY_ID")
        api_key_private_key = os.getenv("CDP_API_KEY_SECRET", "").replace('\\n', '\n')
        wallet_secret = os.getenv("CDP_WALLET_SECRET")

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
                    logger.info(f"Loading existing wallet: {wallet_address}")
            except Exception as e:
                logger.error(f"Could not load wallet data: {e}")

        config = CdpEvmWalletProviderConfig(
            network_id=NETWORK_ID,
            address=wallet_address,
            idempotency_key="sentinel_alpha_main_wallet",
            rpc_url=RPC_URL,
        )
        provider = CdpEvmWalletProvider(config)

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
        for name, action in self.actions.items():
            if name.endswith(keyword):
                return action
        return None

    # ── Price History ─────────────────────────────────────────────────────────

    def _load_price_history(self):
        """Load price history from disk. Migrates old flat-list format automatically."""
        path = "price_history.json"
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                raw = json.load(f)

            # Migrate old format: top-level list → BTC/ETH dict
            if isinstance(raw, list):
                logger.info("Migrating price_history.json from flat list to multi-pair format.")
                raw = {
                    "BTC/ETH": [
                        {
                            "timestamp": r["timestamp"],
                            "base_price": r["btc_price"],
                            "quote_price": r["eth_price"],
                        }
                        for r in raw
                    ]
                }

            cutoff = datetime.now() - timedelta(hours=WINDOW_SIZE_HOURS)
            for pair in self.pairs:
                records = raw.get(pair.name, [])
                if not records:
                    continue
                df = pd.DataFrame(records)
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df["base_price"] = df["base_price"].astype(float)
                df["quote_price"] = df["quote_price"].astype(float)
                pair.price_history = df[df["timestamp"] > cutoff].reset_index(drop=True)
                logger.info(f"[{pair.name}] Loaded {len(pair.price_history)} price points from disk.")
        except Exception as e:
            logger.warning(f"Could not load price history: {e}")

    def _save_price_history(self):
        """Persist all pairs' price history to disk in multi-pair dict format."""
        try:
            data = {}
            for pair in self.pairs:
                if len(pair.price_history) == 0:
                    continue
                data[pair.name] = json.loads(
                    pair.price_history.to_json(orient="records", date_format="iso")
                )
            with open("price_history.json", "w") as f:
                json.dump(data, f)
        except Exception as e:
            logger.warning(f"Could not save price history: {e}")

    # ── Prices ────────────────────────────────────────────────────────────────

    def fetch_prices(self):
        """Fetch spot prices for all unique symbols across active pairs, update histories."""
        import requests
        try:
            # Deduplicate symbols so ETH-USD is fetched once even if used by multiple pairs
            symbols = {sym for pair in self.pairs if pair.is_enabled()
                       for sym in (pair.base_symbol, pair.quote_symbol)}
            prices: dict[str, float] = {}
            for sym in symbols:
                resp = requests.get(
                    f"https://api.coinbase.com/v2/prices/{sym}-USD/spot", timeout=10
                ).json()
                prices[sym] = float(resp['data']['amount'])

            now = datetime.now()
            cutoff = now - timedelta(hours=WINDOW_SIZE_HOURS)

            for pair in self.pairs:
                if not pair.is_enabled():
                    continue
                new_row = pd.DataFrame([{
                    "timestamp": now,
                    "base_price": prices[pair.base_symbol],
                    "quote_price": prices[pair.quote_symbol],
                }])
                pair.price_history = pd.concat(
                    [pair.price_history, new_row], ignore_index=True
                )
                pair.price_history = pair.price_history[
                    pair.price_history["timestamp"] > cutoff
                ]

            price_log = " | ".join(
                f"{sym}: ${prices[sym]:,.2f}" for sym in sorted(prices)
            )
            logger.info(f"Price Update — {price_log}")
            self._consecutive_price_failures = 0
            self._save_price_history()

        except Exception as e:
            self._consecutive_price_failures += 1
            logger.error(f"Error fetching prices: {e}")

    # ── Strategy ──────────────────────────────────────────────────────────────

    def calculate_z_score(self, pair: TradingPair) -> Optional[float]:
        """Z-score of base/quote price ratio over the rolling window."""
        if len(pair.price_history) < 2:
            return None
        df = pair.price_history.copy()
        df['ratio'] = df['base_price'] / df['quote_price']
        rolling_mean = df['ratio'].mean()
        rolling_std = df['ratio'].std()
        if rolling_std == 0 or np.isnan(rolling_std):
            return 0.0
        return float((df['ratio'].iloc[-1] - rolling_mean) / rolling_std)

    def _check_cointegration(self, pair: TradingPair):
        """Engle-Granger cointegration test, cached per pair (~hourly refresh)."""
        n = len(pair.price_history)
        if n < COINT_MIN_POINTS:
            return False, None
        if pair._coint_p_value is not None and n - pair._coint_last_size < 12:
            return pair._coint_p_value < COINT_P_THRESHOLD, pair._coint_p_value
        try:
            base  = pair.price_history["base_price"].values
            quote = pair.price_history["quote_price"].values
            _, p_value, _ = coint(base, quote)
            pair._coint_p_value = p_value
            pair._coint_last_size = n
            valid = p_value < COINT_P_THRESHOLD
            logger.info(
                f"[{pair.name}] Cointegration check: p={p_value:.4f} "
                f"({'VALID' if valid else 'INVALID — trading paused'})"
            )
            return valid, p_value
        except Exception as e:
            logger.warning(f"[{pair.name}] Cointegration test failed: {e}. Failing open.")
            return True, None

    def _adaptive_threshold(self, pair: TradingPair) -> float:
        """Z-score threshold scaled to current volatility regime."""
        if len(pair.price_history) < ADAPTIVE_THRESHOLD_WINDOW + 2:
            return Z_SCORE_THRESHOLD
        ratio = pair.price_history["base_price"] / pair.price_history["quote_price"]
        full_std = ratio.std()
        recent_std = ratio.iloc[-ADAPTIVE_THRESHOLD_WINDOW:].std()
        if full_std == 0 or np.isnan(full_std) or np.isnan(recent_std):
            return Z_SCORE_THRESHOLD
        scale = max(0.5, min(2.0, recent_std / full_std))
        return Z_SCORE_THRESHOLD * scale

    def _scaled_trade_pct(self, z_score: float) -> float:
        excess = abs(z_score) - Z_SCORE_THRESHOLD
        scale = max(0.0, min(1.0, excess / TRADE_SCALE_RAMP))
        return TRADE_SIZE_PCT + scale * (TRADE_SIZE_MAX_PCT - TRADE_SIZE_PCT)

    # ── Permit2 approval ──────────────────────────────────────────────────────

    PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
    ERC20_ALLOWANCE_ABI = [
        {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
         "name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
        {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
         "name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    ]

    def _ensure_permit2_approval(self, token_address: str) -> bool:
        """Ensure token has max Permit2 allowance. Returns True if already approved or approval succeeded."""
        if token_address == NATIVE_ETH:
            return True
        try:
            w3 = self.wallet_provider._web3
            wallet = self.wallet_provider.get_address()
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(token_address),
                abi=self.ERC20_ALLOWANCE_ABI,
            )
            allowance = contract.functions.allowance(
                Web3.to_checksum_address(wallet),
                Web3.to_checksum_address(self.PERMIT2_ADDRESS),
            ).call()
            if allowance > 0:
                return True
            logger.info(f"Approving {token_address} for Permit2 (allowance=0)...")
            approve_data = contract.encode_abi("approve",
                args=[Web3.to_checksum_address(self.PERMIT2_ADDRESS), 2**256 - 1],
            )
            tx_hash = self.wallet_provider.send_transaction({
                "to": Web3.to_checksum_address(token_address),
                "data": approve_data,
                "value": 0,
            })
            receipt = self.wallet_provider.wait_for_transaction_receipt(tx_hash)
            status = receipt.get("status", 0) if isinstance(receipt, dict) else getattr(receipt, "status", 0)
            if status in (1, "success"):
                logger.info(f"Permit2 approval confirmed for {token_address} | tx: {tx_hash}")
                return True
            else:
                logger.error(f"Permit2 approval failed for {token_address} | tx: {tx_hash}")
                return False
        except Exception as e:
            logger.error(f"Error ensuring Permit2 approval for {token_address}: {e}")
            return False

    # ── Aave V3 yield ─────────────────────────────────────────────────────────

    @staticmethod
    def _tx_succeeded(receipt) -> bool:
        status = receipt.get("status", 0) if isinstance(receipt, dict) else getattr(receipt, "status", 0)
        return status in (1, "success")

    def _notify(self, message: str):
        """Send a Telegram message. Failures are logged but never affect trading."""
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            return
        try:
            import requests as _req
            _req.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
                timeout=5,
            )
        except Exception as e:
            logger.warning(f"Telegram notification failed: {e}")

    def _get_aave_eth_balance(self) -> float:
        """Returns ETH currently earning yield in Aave (aWETH balance), or 0 if disabled."""
        if not AAVE_ENABLED:
            return 0.0
        try:
            w3 = self.wallet_provider._web3
            wallet = Web3.to_checksum_address(self.wallet_provider.get_address())
            aweth = w3.eth.contract(
                address=Web3.to_checksum_address(AAVE_AWETH_ADDRESS),
                abi=[{"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf",
                      "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}],
            )
            value = float(aweth.functions.balanceOf(wallet).call()) / 1e18
            self._aave_balance_cache = value
            return value
        except Exception as e:
            logger.error(f"[Aave] Error reading aWETH balance: {e}")
            if self._aave_balance_cache is not None:
                logger.warning(f"[Aave] Returning stale cached aWETH balance: {self._aave_balance_cache:.6f}")
                self._stale_balance_used = True
                return self._aave_balance_cache
            return 0.0

    def _aave_supply_eth(self, amount_eth: float) -> bool:
        """Wrap ETH → WETH, approve Aave Pool if needed, supply. Returns True on success."""
        if not AAVE_ENABLED or DRY_RUN or amount_eth <= 0:
            return True
        try:
            w3 = self.wallet_provider._web3
            wallet = Web3.to_checksum_address(self.wallet_provider.get_address())
            weth_addr = Web3.to_checksum_address(TOKENS[NETWORK_ID]["WETH"])
            pool_addr = Web3.to_checksum_address(AAVE_POOL_ADDRESS)
            amount_wei = int(amount_eth * 1e18)
            weth = w3.eth.contract(address=weth_addr, abi=WETH_ABI)

            # 1. Wrap ETH → WETH
            tx = self.wallet_provider.send_transaction(
                {"to": weth_addr, "data": weth.encode_abi("deposit", args=[]), "value": amount_wei}
            )
            if not self._tx_succeeded(self.wallet_provider.wait_for_transaction_receipt(tx)):
                logger.error(f"[Aave] ETH wrap failed | tx: {tx}")
                return False

            # 2. Approve Pool to spend WETH (skip if already sufficient)
            if weth.functions.allowance(wallet, pool_addr).call() < amount_wei:
                tx = self.wallet_provider.send_transaction(
                    {"to": weth_addr, "data": weth.encode_abi("approve", args=[pool_addr, 2**256 - 1]), "value": 0}
                )
                if not self._tx_succeeded(self.wallet_provider.wait_for_transaction_receipt(tx)):
                    logger.error(f"[Aave] WETH→Pool approval failed | tx: {tx}")
                    return False

            # 3. Supply WETH to Pool
            pool = w3.eth.contract(address=pool_addr, abi=AAVE_POOL_ABI)
            tx = self.wallet_provider.send_transaction(
                {"to": pool_addr, "data": pool.encode_abi("supply", args=[weth_addr, amount_wei, wallet, 0]), "value": 0}
            )
            if not self._tx_succeeded(self.wallet_provider.wait_for_transaction_receipt(tx)):
                logger.error(f"[Aave] Supply failed | tx: {tx}")
                return False

            self._balance_cache.pop(NATIVE_ETH, None)
            logger.info(f"[Aave] Supplied {amount_eth:.6f} ETH. aWETH balance: {self._get_aave_eth_balance():.6f}")
            return True
        except Exception as e:
            logger.error(f"[Aave] Supply error: {e}")
            return False

    def _aave_withdraw_eth(self, amount_eth: float, _retry: bool = True) -> bool:
        """Withdraw WETH from Aave Pool, unwrap to native ETH. Returns True on success."""
        if not AAVE_ENABLED or DRY_RUN or amount_eth <= 0:
            return True
        try:
            w3 = self.wallet_provider._web3
            wallet = Web3.to_checksum_address(self.wallet_provider.get_address())
            weth_addr = Web3.to_checksum_address(TOKENS[NETWORK_ID]["WETH"])
            pool_addr = Web3.to_checksum_address(AAVE_POOL_ADDRESS)
            amount_wei = int(amount_eth * 1e18)

            # 1. Withdraw WETH from Pool (burns aWETH, sends WETH to wallet)
            pool = w3.eth.contract(address=pool_addr, abi=AAVE_POOL_ABI)
            tx = self.wallet_provider.send_transaction(
                {"to": pool_addr, "data": pool.encode_abi("withdraw", args=[weth_addr, amount_wei, wallet]), "value": 0}
            )
            if not self._tx_succeeded(self.wallet_provider.wait_for_transaction_receipt(tx)):
                logger.error(f"[Aave] Withdraw failed | tx: {tx}")
                return False

            # 2. Unwrap WETH → ETH
            weth = w3.eth.contract(address=weth_addr, abi=WETH_ABI)
            tx = self.wallet_provider.send_transaction(
                {"to": weth_addr, "data": weth.encode_abi("withdraw", args=[amount_wei]), "value": 0}
            )
            if not self._tx_succeeded(self.wallet_provider.wait_for_transaction_receipt(tx)):
                logger.error(f"[Aave] WETH unwrap failed | tx: {tx}")
                return False

            self._balance_cache.pop(NATIVE_ETH, None)
            logger.info(f"[Aave] Withdrew {amount_eth:.6f} ETH. aWETH balance: {self._get_aave_eth_balance():.6f}")
            return True
        except Exception as e:
            logger.error(f"[Aave] Withdraw error: {e}")
            # CDP sometimes returns "Nonce too low" when the local nonce drifts out of
            # sync with the chain (e.g. after a failed or dropped transaction).
            # Re-creating the transaction once is sufficient to resync.
            if _retry and "nonce too low" in str(e).lower():
                logger.info("[Aave] Retrying withdraw after nonce error...")
                time.sleep(2)
                return self._aave_withdraw_eth(amount_eth, _retry=False)
            return False

    def _deposit_idle_eth(self):
        """Deposit ETH above reserve into Aave for yield. No-op if disabled or dry run."""
        if not AAVE_ENABLED or DRY_RUN:
            return
        try:
            eth_balance = self.get_token_balance(NATIVE_ETH, force_refresh=True)
            idle = eth_balance - AAVE_ETH_RESERVE
            if idle >= AAVE_MIN_DEPOSIT:
                logger.info(f"[Aave] Depositing {idle:.6f} idle ETH (keeping {AAVE_ETH_RESERVE} ETH reserve).")
                self._aave_supply_eth(idle)
        except Exception as e:
            logger.error(f"[Aave] Error depositing idle ETH: {e}")

    def _ensure_eth_available(self, amount_needed: float):
        """Withdraw from Aave if native ETH balance is insufficient for a pending trade."""
        if not AAVE_ENABLED or DRY_RUN:
            return
        try:
            eth_balance = self.get_token_balance(NATIVE_ETH, force_refresh=True)
            shortfall = amount_needed - eth_balance
            if shortfall <= 0:
                return
            aave_bal = self._get_aave_eth_balance()
            withdraw_amount = min(shortfall + AAVE_ETH_RESERVE, aave_bal)
            if withdraw_amount > 0:
                logger.info(f"[Aave] Withdrawing {withdraw_amount:.6f} ETH for trade (shortfall={shortfall:.6f}).")
                self._aave_withdraw_eth(withdraw_amount)
        except Exception as e:
            logger.error(f"[Aave] Error ensuring ETH available: {e}")

    # ── Portfolio ─────────────────────────────────────────────────────────────

    BALANCE_CACHE_TTL = int(os.getenv("BALANCE_CACHE_TTL", 300))  # seconds; default 5 min

    def get_token_balance(self, token_address: str, force_refresh: bool = False) -> float:
        now = time.time()
        cached = self._balance_cache.get(token_address)
        if not force_refresh and cached and (now - cached[1]) < self.BALANCE_CACHE_TTL:
            return cached[0]
        try:
            if token_address == NATIVE_ETH:
                value = float(self.wallet_provider.get_balance()) / 1e18
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
                    value = float(balance_wei) / (10 ** decimals)
                else:
                    result = balance_tool.invoke({"contract_address": token_address})
                    import re
                    match = re.search(r"is ([\d\.]+)", result)
                    value = float(match.group(1)) if match else 0.0
            self._balance_cache[token_address] = (value, now)
            return value
        except Exception as e:
            logger.error(f"Error getting balance for {token_address}: {e}")
            # Return stale cache on error rather than 0.0, if available
            if cached:
                logger.warning(f"Returning stale cached balance for {token_address}")
                self._stale_balance_used = True
                return cached[0]
            return 0.0

    def _get_portfolio_eth_value(self) -> float:
        """Total portfolio in ETH-equivalent: native ETH + all base tokens converted via latest prices."""
        # Find ETH USD price from any pair with quote_symbol=ETH that has data
        eth_price_usd = 0.0
        for pair in self.pairs:
            if pair.quote_symbol == "ETH" and len(pair.price_history) > 0:
                eth_price_usd = float(pair.price_history.iloc[-1]['quote_price'])
                break

        eth_balance = self.get_token_balance(NATIVE_ETH)
        aave_eth = self._get_aave_eth_balance()
        if eth_price_usd == 0:
            return eth_balance + aave_eth  # fallback before first price fetch

        total = eth_balance + aave_eth
        seen_tokens: set[str] = set()
        for pair in self.pairs:
            if not pair.is_enabled() or pair.base_token in seen_tokens:
                continue
            if len(pair.price_history) == 0:
                continue
            seen_tokens.add(pair.base_token)
            base_price_usd = float(pair.price_history.iloc[-1]['base_price'])
            token_balance = self.get_token_balance(pair.base_token)
            total += token_balance * base_price_usd / eth_price_usd
        return total

    # ── Risk Controls ─────────────────────────────────────────────────────────

    # How long to wait with a confirmed stop-loss before halting (gives time to
    # distinguish a real loss from a transient data glitch).
    STOP_LOSS_CONFIRM_SECS = 1800  # 30 minutes

    def check_stop_loss(self) -> bool:
        today_utc = datetime.utcnow().date()

        # Reset the stale-balance flag before each evaluation so it only reflects
        # calls made during this cycle.
        self._stale_balance_used = False

        portfolio_value = self._get_portfolio_eth_value()

        # ── Network/data quality guard ────────────────────────────────────────
        # If prices failed to fetch OR any balance fell back to stale cache,
        # our portfolio value is unreliable.  Skip the stop-loss check entirely
        # this cycle rather than risk a false halt.
        if self._consecutive_price_failures > 0 or self._stale_balance_used:
            logger.warning(
                f"Stop-loss check skipped — stale data "
                f"(price_failures={self._consecutive_price_failures}, "
                f"stale_balance={self._stale_balance_used}). "
                f"Will re-evaluate once connectivity is restored."
            )
            return True

        if self.initial_daily_balance is None or self.daily_reset_date != today_utc:
            self.initial_daily_balance = portfolio_value
            self.daily_reset_date = today_utc
            self._stop_loss_first_seen = None
            logger.info(f"Daily balance reset: {portfolio_value:.6f} ETH-equiv")
            return True

        if self.initial_daily_balance == 0:
            return True

        drop = (self.initial_daily_balance - portfolio_value) / self.initial_daily_balance
        if drop > DAILY_STOP_LOSS_PCT:
            now = datetime.utcnow()
            if self._stop_loss_first_seen is None:
                # First detection — start the confirmation countdown.
                self._stop_loss_first_seen = now
                logger.warning(
                    f"Stop-loss threshold breached ({drop:.2%} drop). "
                    f"Trading paused. Will confirm and halt in "
                    f"{self.STOP_LOSS_CONFIRM_SECS // 60} min if loss persists."
                )
                self._notify(
                    f"⚠️ <b>STOP LOSS WARNING</b>\n"
                    f"Portfolio down {drop:.2%} today (threshold: {DAILY_STOP_LOSS_PCT:.0%}).\n"
                    f"Trading paused. Agent will halt in "
                    f"{self.STOP_LOSS_CONFIRM_SECS // 60} min if loss is confirmed."
                )
                # Pause trading this cycle but don't halt yet.
                return False

            elapsed = (now - self._stop_loss_first_seen).total_seconds()
            if elapsed >= self.STOP_LOSS_CONFIRM_SECS:
                logger.critical(
                    f"STOP LOSS CONFIRMED ({drop:.2%} drop sustained for "
                    f"{elapsed / 60:.0f} min). Halting agent."
                )
                self._notify(
                    f"🚨 <b>STOP LOSS TRIGGERED</b>\n"
                    f"Portfolio dropped {drop:.2%} today (sustained "
                    f"{elapsed / 60:.0f} min).\n"
                    f"Agent has halted. Manual restart required."
                )
                import sys
                sys.exit(1)
            else:
                logger.warning(
                    f"Stop-loss still breached ({drop:.2%}). "
                    f"Halting in {(self.STOP_LOSS_CONFIRM_SECS - elapsed) / 60:.0f} min "
                    f"if loss persists."
                )
                return False

        # Loss recovered — reset the cooldown timer.
        if self._stop_loss_first_seen is not None:
            logger.info("Stop-loss condition cleared — resuming normal trading.")
            self._stop_loss_first_seen = None
        return True

    def _seed_trade_state(self):
        """Restore per-pair cooldown state from trades.json across restarts."""
        if not os.path.exists(TRADES_FILE):
            return
        pair_map = {p.name: p for p in self.pairs}
        try:
            cutoff = datetime.now() - timedelta(days=1)
            with open(TRADES_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        pair_name = record.get("pair", "BTC/ETH")
                        pair = pair_map.get(pair_name)
                        if pair is None:
                            continue
                        ts = datetime.fromisoformat(record["timestamp"])
                        if ts > cutoff:
                            pair.trades_in_last_24h.append(ts)
                        if ts > pair.last_trade_time:
                            pair.last_trade_time = ts
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
            for pair in self.pairs:
                if pair.trades_in_last_24h:
                    logger.info(
                        f"[{pair.name}] Seeded {len(pair.trades_in_last_24h)} recent trades. "
                        f"Last trade: {pair.last_trade_time}"
                    )
        except Exception as e:
            logger.warning(f"Could not seed trade state: {e}")

    # ── Logging & Summaries ───────────────────────────────────────────────────

    def _log_trade_structured(self, pair: TradingPair, signal: str, z_score: float,
                               trade_amount: float, actual_from=None, actual_to=None):
        """Append a structured JSONL record to trades.json."""
        if len(pair.price_history) == 0:
            return
        latest = pair.price_history.iloc[-1]
        base_price  = float(latest['base_price'])
        quote_price = float(latest['quote_price'])
        ratio = base_price / quote_price if quote_price > 0 else 0

        slippage_pct = None
        if actual_to is not None and quote_price > 0 and base_price > 0:
            expected_to = (trade_amount * quote_price / base_price if signal == "BUY"
                           else trade_amount * base_price / quote_price)
            if expected_to > 0:
                slippage_pct = round((actual_to - expected_to) / expected_to * 100, 4)

        portfolio_eth_value = self._get_portfolio_eth_value()

        entry = {
            "timestamp": datetime.now().isoformat(),
            "pair": pair.name,
            "signal": signal,
            "z_score": round(z_score, 6),
            # Keep btc_price/eth_price keys for dashboard backward compat
            "btc_price": base_price,
            "eth_price": quote_price,
            "base_price": base_price,
            "quote_price": quote_price,
            "ratio": round(ratio, 6),
            "trade_amount": trade_amount,
            "actual_from_amount": actual_from,
            "actual_to_amount": actual_to,
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
        """Write one summary record per pair to daily_summary.json, then reset counters."""
        portfolio_eth_value = self._get_portfolio_eth_value()
        stop_loss_proximity_pct = None
        if self.initial_daily_balance and self.initial_daily_balance > 0:
            floor = self.initial_daily_balance * (1 - DAILY_STOP_LOSS_PCT)
            stop_loss_proximity_pct = round(
                (portfolio_eth_value - floor) / self.initial_daily_balance * 100, 2
            )

        date_str = self.last_summary_date.isoformat()

        for pair in self.pairs:
            if not pair.z_scores_today:
                pair.reset_daily_counters()
                continue

            latest = pair.price_history.iloc[-1] if len(pair.price_history) > 0 else None
            summary = {
                "date": date_str,
                "pair": pair.name,
                "trades_executed": pair.trades_executed_today,
                "signals_ignored_cooldown": pair.signals_ignored_cooldown_today,
                "signals_ignored_limit": pair.signals_ignored_limit_today,
                "z_score_min": round(min(pair.z_scores_today), 4),
                "z_score_max": round(max(pair.z_scores_today), 4),
                "z_score_mean": round(sum(pair.z_scores_today) / len(pair.z_scores_today), 4),
                "portfolio_eth_value_eod": round(portfolio_eth_value, 8),
                "stop_loss_proximity_pct": stop_loss_proximity_pct,
                "btc_price_eod": float(latest['base_price']) if latest is not None else None,
                "eth_price_eod": float(latest['quote_price']) if latest is not None else None,
            }
            try:
                with open(DAILY_SUMMARY_FILE, "a") as f:
                    f.write(json.dumps(summary) + "\n")
            except Exception as e:
                logger.error(f"Failed to write daily summary for {pair.name}: {e}")

            logger.info(
                f"[{pair.name}] Daily summary: {pair.trades_executed_today} trades, "
                f"portfolio={portfolio_eth_value:.6f} ETH-equiv"
            )
            self._notify(
                f"📊 <b>Daily Summary [{pair.name}]</b> — {date_str}\n"
                f"Trades: {pair.trades_executed_today} | Ignored: {pair.signals_ignored_cooldown_today + pair.signals_ignored_limit_today}\n"
                f"Z-Score range: {min(pair.z_scores_today):+.2f} → {max(pair.z_scores_today):+.2f}\n"
                f"Portfolio: {portfolio_eth_value:.6f} ETH-equiv"
            )
            pair.reset_daily_counters()

    # ── Trade Execution ───────────────────────────────────────────────────────

    def execute_trade(self, pair: TradingPair, signal: str, z_score: float = 0.0):
        """Execute or shadow-log a trade for the given pair."""
        now = datetime.now()
        pair.trades_in_last_24h = [
            t for t in pair.trades_in_last_24h if now - t < timedelta(days=1)
        ]

        if len(pair.trades_in_last_24h) >= 50:
            logger.warning(f"[{pair.name}] Daily trade limit reached. Ignoring {signal}.")
            pair.signals_ignored_limit_today += 1
            return

        if now - pair.last_trade_time < COOLDOWN_PERIOD:
            logger.info(f"[{pair.name}] {signal} ignored — cooldown active.")
            pair.signals_ignored_cooldown_today += 1
            return

        try:
            scaled_pct = self._scaled_trade_pct(z_score)

            if signal == "BUY":
                # Swap quote (ETH) → base token
                from_token = pair.quote_token
                to_token   = pair.base_token
                native_bal = self.get_token_balance(from_token, force_refresh=True)
                aave_eth   = self._get_aave_eth_balance()
                total_eth  = native_bal + aave_eth
                trade_amount = max(total_eth * scaled_pct * pair.capital_fraction, MIN_TRADE_ETH)
                self._ensure_eth_available(trade_amount)
                balance = self.get_token_balance(from_token, force_refresh=True)
                if balance < trade_amount:
                    logger.warning(f"[{pair.name}] Insufficient quote balance ({balance:.6f}). Skipping.")
                    return
                logger.info(
                    f"[{pair.name}] SIGNAL: BUY {pair.base_symbol} "
                    f"({pair.quote_symbol} → {pair.base_symbol}). "
                    f"Amount: {trade_amount:.6f} {pair.quote_symbol} "
                    f"({scaled_pct*100:.1f}% × {pair.capital_fraction:.0%} of balance, "
                    f"Z={z_score:.2f}, DRY_RUN={DRY_RUN})"
                )

            elif signal == "SELL":
                # Swap base token → quote (ETH)
                from_token = pair.base_token
                to_token   = pair.quote_token
                balance    = self.get_token_balance(from_token, force_refresh=True)
                trade_amount = max(balance * scaled_pct * pair.capital_fraction, pair.min_base_amount)
                if balance < trade_amount:
                    logger.warning(f"[{pair.name}] Insufficient base balance ({balance:.8f}). Skipping.")
                    return
                logger.info(
                    f"[{pair.name}] SIGNAL: SELL {pair.base_symbol} "
                    f"({pair.base_symbol} → {pair.quote_symbol}). "
                    f"Amount: {trade_amount:.8f} {pair.base_symbol} "
                    f"({scaled_pct*100:.1f}% × {pair.capital_fraction:.0%} of balance, "
                    f"Z={z_score:.2f}, DRY_RUN={DRY_RUN})"
                )
            else:
                return

            actual_from = None
            actual_to   = None

            if not DRY_RUN:
                # Ensure Permit2 allowance for ERC-20 sells before swapping
                if signal == "SELL" and not self._ensure_permit2_approval(from_token):
                    logger.error(f"[{pair.name}] Aborting swap: could not approve {from_token} for Permit2.")
                    return
                swap_tool = self._get_action("swap")
                if not swap_tool:
                    logger.error(f"Swap tool not found. Available: {list(self.actions.keys())}")
                    return
                result_str = swap_tool.invoke({
                    "from_token": from_token,
                    "to_token": to_token,
                    "from_amount": str(trade_amount),
                    "slippage_bps": SWAP_SLIPPAGE_BPS,
                })
                try:
                    result = json.loads(result_str)
                except (json.JSONDecodeError, TypeError):
                    result = {"success": False, "error": f"Unparseable: {result_str}"}

                if not result.get("success"):
                    logger.error(f"[{pair.name}] Swap failed: {result.get('error', 'unknown')}")
                    return

                try:
                    actual_from = float(result.get("fromAmount") or 0) or None
                    actual_to   = float(result.get("toAmount")   or 0) or None
                except (TypeError, ValueError):
                    pass

                logger.info(
                    f"[{pair.name}] Swap executed: {actual_from} {result.get('fromTokenName')} "
                    f"→ {actual_to} {result.get('toTokenName')} | tx: {result.get('transactionHash')}"
                )

            pair.last_trade_time = now
            pair.trades_in_last_24h.append(now)
            pair.trades_executed_today += 1
            self._log_trade_structured(pair, signal, z_score, trade_amount, actual_from, actual_to)
            emoji = "🟢" if signal == "BUY" else "🔴"
            self._notify(
                f"{emoji} <b>{signal} {pair.base_symbol}</b> [{pair.name}]\n"
                f"Amount: {trade_amount:.6f} {pair.quote_symbol if signal == 'BUY' else pair.base_symbol}\n"
                f"Z-Score: {z_score:+.4f} | DRY_RUN: {DRY_RUN}"
            )

        except Exception as e:
            logger.error(f"[{pair.name}] Error in execution: {e}")

    # ── Main Loop ─────────────────────────────────────────────────────────────

    def run(self):
        active = [p.name for p in self.pairs if p.is_enabled()]
        logger.info(
            f"Sentinel-Alpha started. Strategy: Mean Reversion | "
            f"Pairs: {active} | DRY_RUN: {DRY_RUN}"
        )
        if AAVE_ENABLED:
            logger.info(f"[Aave] Yield enabled. Current aWETH balance: {self._get_aave_eth_balance():.6f} ETH")
        self._notify(
            f"🤖 <b>Sentinel-Alpha started</b>\n"
            f"Network: {NETWORK_ID} | DRY_RUN: {DRY_RUN}\n"
            f"Pairs: {', '.join(active)}\n"
            f"Aave yield: {'enabled' if AAVE_ENABLED else 'disabled'}"
        )

        while True:
            try:
                today = datetime.now().date()
                if today != self.last_summary_date:
                    self._write_daily_summary()
                    self.last_summary_date = today

                # Fetch all prices first, then evaluate risk and signals
                self.fetch_prices()

                if not self.check_stop_loss():
                    time.sleep(300)
                    continue

                for pair in self.pairs:
                    if not pair.is_enabled():
                        continue

                    z_score = self.calculate_z_score(pair)
                    if z_score is None:
                        logger.info(f"[{pair.name}] Collecting initial data points...")
                        continue

                    pair.z_scores_today.append(z_score)
                    threshold = self._adaptive_threshold(pair)
                    coint_valid, coint_p = self._check_cointegration(pair)
                    p_str = f"{coint_p:.4f}" if coint_p is not None else "N/A"

                    logger.info(
                        f"[{pair.name}] Current Z-Score: {z_score:.4f} "
                        f"(Points: {len(pair.price_history)}, "
                        f"Threshold: {threshold:.2f}, Coint-p: {p_str})"
                    )

                    if not coint_valid:
                        logger.info(f"[{pair.name}] Cointegration gate: pair not cointegrated — no trades.")
                    elif z_score < -threshold:
                        self.execute_trade(pair, "BUY", z_score)
                    elif z_score > threshold:
                        self.execute_trade(pair, "SELL", z_score)

                self._deposit_idle_eth()
                time.sleep(300)

            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                self._notify(f"⚠️ <b>Agent error</b>\n{str(e)[:200]}")
                time.sleep(60)


if __name__ == "__main__":
    agent = SentinelAlpha()
    agent.run()
