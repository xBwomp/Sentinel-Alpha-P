"""
test_swap.py — Force a small test swap (ETH -> cbBTC) to verify the swap pipeline works.
Uses the same wallet and config as main.py. Swaps a fixed 0.0001 ETH.
"""

import json
import logging
import os

from dotenv import load_dotenv

from coinbase_agentkit import AgentKit, AgentKitConfig
from coinbase_agentkit.action_providers import (
    cdp_evm_wallet_action_provider,
    erc20_action_provider,
)
from coinbase_agentkit.wallet_providers import CdpEvmWalletProvider, CdpEvmWalletProviderConfig

load_dotenv()

NETWORK_ID = os.getenv("NETWORK_ID", "base-mainnet")
WALLET_DATA_FILE = os.getenv("WALLET_DATA_FILE", "wallet_data.json")
NATIVE_ETH = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
TOKENS = {
    "base-mainnet": {"cbBTC": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"},
    "base-sepolia": {"cbBTC": "0xcbB7C0006F23900c38EB856149F799620fcb8A4a"},
}
TEST_AMOUNT_ETH = "0.0001"  # ~$0.25 — adjust if needed

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("test_swap")


def init_wallet():
    api_key_name = os.getenv("CDP_API_KEY_ID")
    api_key_private_key = os.getenv("CDP_API_KEY_SECRET", "").replace("\\n", "\n")
    wallet_secret = os.getenv("CDP_WALLET_SECRET")

    os.environ["CDP_API_KEY_ID"] = api_key_name
    os.environ["CDP_API_KEY_SECRET"] = api_key_private_key
    os.environ["CDP_WALLET_SECRET"] = wallet_secret

    wallet_address = None
    if os.path.exists(WALLET_DATA_FILE):
        try:
            with open(WALLET_DATA_FILE) as f:
                wallet_address = json.load(f).get("address")
        except Exception as e:
            log.warning(f"Could not load wallet_data.json: {e}")

    config = CdpEvmWalletProviderConfig(
        network_id=NETWORK_ID,
        address=wallet_address,
        idempotency_key="sentinel_alpha_main_wallet",
    )
    provider = CdpEvmWalletProvider(config)
    log.info(f"Wallet: {provider.get_address()} on {NETWORK_ID}")
    return provider


def get_eth_balance(provider):
    return float(provider.get_balance()) / 1e18


def find_action(actions, suffix):
    for a in actions:
        if a.name.endswith(suffix):
            return a
    return None


def main():
    log.info("=== Sentinel Alpha — Swap Test ===")

    if NETWORK_ID not in ("base-mainnet", "ethereum-mainnet"):
        log.error(f"CDP Swap API does not support {NETWORK_ID}. Set NETWORK_ID=base-mainnet.")
        return

    provider = init_wallet()

    eth_balance = get_eth_balance(provider)
    log.info(f"ETH balance: {eth_balance:.6f} ETH")

    if eth_balance < float(TEST_AMOUNT_ETH) * 1.01:  # 1% buffer for gas
        log.error(
            f"Insufficient ETH. Need >{TEST_AMOUNT_ETH} ETH + gas, have {eth_balance:.6f} ETH."
        )
        return

    kit = AgentKit(
        AgentKitConfig(
            wallet_provider=provider,
            action_providers=[
                cdp_evm_wallet_action_provider(),
                erc20_action_provider(),
            ],
        )
    )
    actions = kit.get_actions()
    log.info(f"Available actions: {[a.name for a in actions]}")

    cbbtc_address = TOKENS[NETWORK_ID]["cbBTC"]

    # --- Step 1: get a price quote first (dry-run check) ---
    log.info(f"Fetching swap price for {TEST_AMOUNT_ETH} ETH -> cbBTC ...")
    price_action = find_action(actions, "get_swap_price")
    if price_action:
        price_str = price_action.invoke(
            {"from_token": NATIVE_ETH, "to_token": cbbtc_address, "from_amount": TEST_AMOUNT_ETH}
        )
        try:
            price = json.loads(price_str)
        except Exception:
            price = {"success": False, "error": price_str}

        if not price.get("success"):
            log.error(f"Price quote failed: {price.get('error')}")
            return
        log.info(
            f"Quote: {price.get('fromAmount')} {price.get('fromTokenName')} "
            f"-> {price.get('toAmount')} {price.get('toTokenName')}"
        )
    else:
        log.warning("get_swap_price action not found, skipping quote step.")

    # --- Step 2: execute the swap ---
    log.info(f"Executing swap: {TEST_AMOUNT_ETH} ETH -> cbBTC ...")
    swap_action = find_action(actions, "swap")
    if not swap_action:
        log.error("swap action not found in available actions.")
        return

    result_str = swap_action.invoke(
        {"from_token": NATIVE_ETH, "to_token": cbbtc_address, "from_amount": TEST_AMOUNT_ETH}
    )
    try:
        result = json.loads(result_str)
    except Exception:
        result = {"success": False, "error": f"Unparseable response: {result_str}"}

    if not result.get("success"):
        log.error(f"SWAP FAILED: {result.get('error', 'unknown error')}")
        return

    log.info("SWAP SUCCEEDED")
    log.info(f"  {result.get('fromAmount')} {result.get('fromTokenName')} -> {result.get('toAmount')} {result.get('toTokenName')}")
    log.info(f"  tx hash : {result.get('transactionHash')}")
    log.info(f"  network : {result.get('network')}")
    log.info(f"  slippage: {result.get('slippageBps')} bps")

    eth_after = get_eth_balance(provider)
    log.info(f"ETH balance after: {eth_after:.6f} ETH (spent {eth_balance - eth_after:.6f})")


if __name__ == "__main__":
    main()
