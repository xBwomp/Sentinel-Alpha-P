import json
import logging
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Deque, Dict, Optional, Tuple

import pandas as pd
import requests
from dotenv import load_dotenv

try:
    from coinbase_agentkit import CdpWalletProvider
except ImportError:  # pragma: no cover
    CdpWalletProvider = None


@dataclass
class Config:
    pair: str = os.getenv("TRADING_PAIR", "BTC-USD")
    price_poll_seconds: int = int(os.getenv("PRICE_POLL_SECONDS", "60"))
    z_window_points: int = int(os.getenv("Z_WINDOW_POINTS", "24"))
    z_buy_threshold: float = float(os.getenv("Z_BUY_THRESHOLD", "-2.0"))
    z_sell_threshold: float = float(os.getenv("Z_SELL_THRESHOLD", "2.0"))
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"
    max_trade_fraction: float = float(os.getenv("MAX_TRADE_FRACTION", "0.02"))
    daily_stop_loss_fraction: float = float(os.getenv("DAILY_STOP_LOSS_FRACTION", "0.05"))
    log_dir: Path = Path(os.getenv("LOG_DIR", "logs"))
    log_file: str = os.getenv("LOG_FILE", "trading_log.txt")
    price_api_url_template: str = os.getenv(
        "PRICE_API_URL_TEMPLATE",
        "https://api.coinbase.com/v2/prices/{pair}/spot",
    )
    cdp_api_key: str = os.getenv("CDP_API_KEY", "")
    cdp_api_secret: str = os.getenv("CDP_API_SECRET", "")
    cdp_wallet_id: str = os.getenv("CDP_WALLET_ID", "")


class SentinelAlpha:
    def __init__(self, config: Config):
        self.config = config
        self.running = True
        self.prices = pd.Series(dtype="float64")
        self.wallet_provider = self._build_wallet_provider()
        self.wallet_value_history: Deque[Tuple[datetime, float]] = deque()

        self._setup_logging()
        self.logger.info("Sentinel-Alpha initialized.")

    def _setup_logging(self) -> None:
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        file_path = self.config.log_dir / self.config.log_file

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s",
            handlers=[
                logging.FileHandler(file_path),
                logging.StreamHandler(sys.stdout),
            ],
        )
        self.logger = logging.getLogger("sentinel_alpha")

    def _build_wallet_provider(self):
        if CdpWalletProvider is None:
            logging.warning(
                "coinbase-agentkit is not installed. Wallet provider disabled; running dry-run only."
            )
            return None

        if not (self.config.cdp_api_key and self.config.cdp_api_secret):
            logging.warning(
                "CDP credentials not found. Wallet provider disabled; running dry-run only."
            )
            return None

        try:
            return CdpWalletProvider(
                api_key_name=self.config.cdp_api_key,
                api_key_private_key=self.config.cdp_api_secret,
                network_id="base-sepolia",
                wallet_id=self.config.cdp_wallet_id or None,
            )
        except Exception as exc:  # pragma: no cover
            logging.error("Failed to initialize CdpWalletProvider: %s", exc)
            return None

    def fetch_price(self) -> Optional[float]:
        url = self.config.price_api_url_template.format(pair=self.config.pair)
        headers = {}
        if self.config.cdp_api_key:
            headers["X-API-Key"] = self.config.cdp_api_key

        try:
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()

            if "data" in data and "amount" in data["data"]:
                return float(data["data"]["amount"])
            if "price" in data:
                return float(data["price"])

            self.logger.error("Unexpected price payload: %s", json.dumps(data)[:500])
            return None
        except Exception as exc:
            self.logger.error("Price fetch failed: %s", exc)
            return None

    def update_series(self, price: float) -> Optional[float]:
        now = datetime.now(timezone.utc)
        self.prices.loc[now] = price

        if len(self.prices) < self.config.z_window_points:
            return None

        self.prices = self.prices.iloc[-self.config.z_window_points :]
        rolling_mean = self.prices.mean()
        rolling_std = self.prices.std(ddof=0)
        if rolling_std == 0:
            return 0.0
        return (price - rolling_mean) / rolling_std

    def get_wallet_value(self) -> float:
        if self.wallet_provider is None:
            return float(os.getenv("SIMULATED_WALLET_USD", "10000"))

        try:
            if hasattr(self.wallet_provider, "get_total_balance_usd"):
                return float(self.wallet_provider.get_total_balance_usd())
            if hasattr(self.wallet_provider, "get_balance"):
                bal = self.wallet_provider.get_balance()
                if isinstance(bal, (int, float)):
                    return float(bal)
                if isinstance(bal, dict):
                    return float(bal.get("usd", bal.get("total_usd", 0.0)))
        except Exception as exc:
            self.logger.error("Failed to get wallet value; using simulated fallback. Error: %s", exc)

        return float(os.getenv("SIMULATED_WALLET_USD", "10000"))

    def check_daily_stop_loss(self, wallet_value: float) -> bool:
        now = datetime.now(timezone.utc)
        self.wallet_value_history.append((now, wallet_value))

        cutoff = now - timedelta(hours=24)
        while self.wallet_value_history and self.wallet_value_history[0][0] < cutoff:
            self.wallet_value_history.popleft()

        if not self.wallet_value_history:
            return False

        day_start_value = self.wallet_value_history[0][1]
        if day_start_value <= 0:
            return False

        drawdown = (day_start_value - wallet_value) / day_start_value
        if drawdown >= self.config.daily_stop_loss_fraction:
            self.logger.critical(
                "Daily stop loss triggered. Drawdown: %.2f%% (threshold %.2f%%). Halting.",
                drawdown * 100,
                self.config.daily_stop_loss_fraction * 100,
            )
            return True
        return False

    def generate_signal(self, z_score: Optional[float]) -> Optional[str]:
        if z_score is None:
            return None
        if z_score < self.config.z_buy_threshold:
            return "BUY"
        if z_score > self.config.z_sell_threshold:
            return "SELL"
        return None

    def execute_shadow_trade(self, signal: str, price: float, wallet_value: float) -> None:
        trade_notional = wallet_value * self.config.max_trade_fraction
        quote_amount = round(trade_notional / price, 8)

        trade_details: Dict[str, object] = {
            "signal": signal,
            "pair": self.config.pair,
            "price": round(price, 2),
            "trade_notional_usd": round(trade_notional, 2),
            "trade_size_asset": quote_amount,
            "dry_run": self.config.dry_run,
            "network": "base-sepolia",
        }

        if self.config.dry_run or self.wallet_provider is None:
            self.logger.info("[SHADOW TRADE] %s", json.dumps(trade_details))
            return

        try:
            if hasattr(self.wallet_provider, "trade"):
                tx = self.wallet_provider.trade(
                    pair=self.config.pair,
                    side=signal.lower(),
                    amount=quote_amount,
                )
                trade_details["tx"] = str(tx)
                self.logger.info("[LIVE TRADE] %s", json.dumps(trade_details))
                return

            self.logger.warning(
                "Wallet provider has no trade() method; falling back to shadow trade. %s",
                json.dumps(trade_details),
            )
        except Exception as exc:
            self.logger.error("Trade execution failed; shadow-only fallback. Error: %s", exc)
            self.logger.info("[SHADOW TRADE] %s", json.dumps(trade_details))

    def run(self) -> None:
        self.logger.info(
            "Starting loop | pair=%s | dry_run=%s | z_window=%s",
            self.config.pair,
            self.config.dry_run,
            self.config.z_window_points,
        )

        while self.running:
            price = self.fetch_price()
            if price is None:
                time.sleep(self.config.price_poll_seconds)
                continue

            wallet_value = self.get_wallet_value()
            if self.check_daily_stop_loss(wallet_value):
                self.running = False
                break

            z_score = self.update_series(price)
            signal = self.generate_signal(z_score)

            self.logger.info(
                "Tick | price=%.2f | z_score=%s | signal=%s | wallet=%.2f",
                price,
                f"{z_score:.4f}" if z_score is not None else "warmup",
                signal or "NONE",
                wallet_value,
            )

            if signal is not None:
                self.execute_shadow_trade(signal, price, wallet_value)

            time.sleep(self.config.price_poll_seconds)


def install_signal_handlers(bot: SentinelAlpha) -> None:
    def _handler(_sig, _frame):
        bot.running = False

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


if __name__ == "__main__":
    load_dotenv()
    sentinel = SentinelAlpha(Config())
    install_signal_handlers(sentinel)
    sentinel.run()
