import os
import json
import time
import requests
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Config from .env
LOG_FILE = os.getenv("LOG_FILE", "trading_log.txt")
WALLET_DATA_FILE = os.getenv("WALLET_DATA_FILE", "wallet_data.json")
TRADES_FILE = os.getenv("TRADES_FILE", "trades.json")
DAILY_SUMMARY_FILE = os.getenv("DAILY_SUMMARY_FILE", "daily_summary.json")
NETWORK_ID = os.getenv("NETWORK_ID", "base-mainnet")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
Z_THRESHOLD = float(os.getenv("Z_SCORE_THRESHOLD", 2.0))
WINDOW_SIZE_HOURS = int(os.getenv("WINDOW_SIZE_HOURS", 24))

# Constants for Balance Checking
RPC_URLS = {
    "base-mainnet": "https://mainnet.base.org",
    "base-sepolia": "https://sepolia.base.org"
}
CBBTC_ADDRESSES = {
    "base-mainnet": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
    "base-sepolia": "0xcbB7C0006F23900c38EB856149F799620fcb8A4a"
}
CBETH_ADDRESSES = {
    "base-mainnet": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22",
    "base-sepolia": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22"
}
ERC20_ABI = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"stateMutability":"view","type":"function"}
]

def load_jsonl(path):
    records = []
    if not os.path.exists(path):
        return records
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def get_performance_metrics():
    from datetime import datetime, timedelta
    trades = load_jsonl(TRADES_FILE)
    summaries = load_jsonl(DAILY_SUMMARY_FILE)

    now = datetime.now()
    today_str = now.date().isoformat()
    week_cutoff = (now - timedelta(days=7)).isoformat()

    # Today's counters from daily summary (in-progress day won't have one yet,
    # so fall back to counting today's trade records directly)
    today_summary = next((s for s in summaries if s["date"] == today_str), None)
    if today_summary:
        trades_today = today_summary.get("trades_executed", 0)
        ignored_today = (
            today_summary.get("signals_ignored_cooldown", 0)
            + today_summary.get("signals_ignored_limit", 0)
        )
    else:
        today_trades = [t for t in trades if t["timestamp"].startswith(today_str)]
        trades_today = len(today_trades)
        ignored_today = 0  # not yet written to summary

    # 7-day paired trades → win rate
    week_trades = sorted(
        [t for t in trades if t["timestamp"] >= week_cutoff],
        key=lambda x: x["timestamp"],
    )
    pairs = []
    open_buy = None
    for t in week_trades:
        if t["signal"] == "BUY" and open_buy is None:
            open_buy = t
        elif t["signal"] == "SELL" and open_buy is not None:
            pairs.append({"open": open_buy, "close": t})
            open_buy = None

    win_rate_7d = None
    if pairs:
        returns = []
        for p in pairs:
            r_entry = p["open"]["ratio"]
            r_exit = p["close"]["ratio"]
            if r_entry > 0:
                returns.append((r_exit - r_entry) / r_entry)
        wins = sum(1 for r in returns if r > 0)
        win_rate_7d = (wins / len(returns) * 100) if returns else None

    # 7-day portfolio return from daily summaries
    week_summaries = sorted(
        [s for s in summaries if s["date"] >= week_cutoff[:10]],
        key=lambda x: x["date"],
    )
    portfolio_return_7d = None
    if len(week_summaries) >= 2:
        sv = week_summaries[0].get("portfolio_eth_value_eod", 0)
        ev = week_summaries[-1].get("portfolio_eth_value_eod", 0)
        if sv > 0:
            portfolio_return_7d = (ev - sv) / sv * 100

    # Open position: last unmatched BUY across all time
    open_position = None
    last_buy = None
    for t in sorted(trades, key=lambda x: x["timestamp"]):
        if t["signal"] == "BUY":
            last_buy = t
        elif t["signal"] == "SELL":
            last_buy = None
    if last_buy:
        open_position = {
            "entry_ratio": last_buy["ratio"],
            "entry_btc_price": last_buy["btc_price"],
            "timestamp": last_buy["timestamp"],
        }

    return {
        "has_data": len(trades) > 0,
        "trades_today": trades_today,
        "ignored_today": ignored_today,
        "completed_pairs_7d": len(pairs),
        "win_rate_7d": win_rate_7d,
        "portfolio_return_7d": portfolio_return_7d,
        "open_position": open_position,
        "unrealized_pnl": None,  # populated in the route once prices are known
    }


def get_wallet_address():
    if os.path.exists(WALLET_DATA_FILE):
        try:
            with open(WALLET_DATA_FILE, 'r') as f:
                return json.load(f).get("address")
        except:
            return "Unknown"
    return "Unknown"

_balance_cache: dict = {"data": None, "ts": 0.0}

def get_balances(address):
    if _balance_cache["data"] is not None and (time.time() - _balance_cache["ts"]) < 60:
        return _balance_cache["data"]

    eth_balance = 0.0
    cbbtc_balance = 0.0
    cbeth_balance = 0.0
    try:
        rpc_url = RPC_URLS.get(NETWORK_ID, RPC_URLS["base-mainnet"])
        w3 = Web3(Web3.HTTPProvider(rpc_url))

        if address and address != "Unknown":
            eth_balance = float(w3.eth.get_balance(address)) / 1e18

            def erc20_balance(token_addr):
                contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
                raw = contract.functions.balanceOf(address).call()
                dec = contract.functions.decimals().call()
                return float(raw) / (10 ** dec)

            cbbtc_addr = CBBTC_ADDRESSES.get(NETWORK_ID)
            if cbbtc_addr:
                cbbtc_balance = erc20_balance(cbbtc_addr)

            cbeth_addr = CBETH_ADDRESSES.get(NETWORK_ID)
            if cbeth_addr:
                cbeth_balance = erc20_balance(cbeth_addr)

    except Exception as e:
        print(f"Error fetching balances: {e}")

    result = (eth_balance, cbbtc_balance, cbeth_balance)
    _balance_cache["data"] = result
    _balance_cache["ts"] = time.time()
    return result

def parse_logs():
    import re
    lines = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r') as f:
            lines = f.readlines()

    recent_logs = [l.strip() for l in lines[-50:]]
    recent_logs.reverse()

    # Collect latest Z-score, z_points, and coint_p per pair from log (scan newest-first)
    pairs_latest = {}
    for line in reversed(lines):
        m = re.search(
            r'\[([^\]]+)\]\s+Current Z-Score:\s*([-\d.]+)\s+\(Points:\s*(\d+).*?Coint-p:\s*([\d.]+)\)',
            line
        )
        if m:
            pair_name = m.group(1)
            if pair_name not in pairs_latest:
                pairs_latest[pair_name] = {
                    'z_score':  float(m.group(2)),
                    'z_points': int(m.group(3)),
                    'coint_p':  float(m.group(4)),
                }

    # Prices from price_history.json
    btc_p = 0.0
    eth_p = 0.0
    cbeth_p = 0.0
    price_history_file = os.getenv("PRICE_HISTORY_FILE", "price_history.json")
    if os.path.exists(price_history_file):
        try:
            with open(price_history_file) as f:
                ph_raw = json.load(f)
            if isinstance(ph_raw, dict):
                btc_eth = ph_raw.get("BTC/ETH", [])
                if btc_eth:
                    btc_p = btc_eth[-1].get("base_price", 0.0)
                    eth_p = btc_eth[-1].get("quote_price", 0.0)
                cbeth_eth = ph_raw.get("cbETH/ETH", [])
                if cbeth_eth:
                    cbeth_p = cbeth_eth[-1].get("base_price", 0.0)
                    if not eth_p:
                        eth_p = cbeth_eth[-1].get("quote_price", 0.0)
            else:
                # Legacy flat list (BTC/ETH only)
                if ph_raw:
                    btc_p = ph_raw[-1].get("btc_price", 0.0)
                    eth_p = ph_raw[-1].get("eth_price", 0.0)
        except Exception:
            pass

    return recent_logs, pairs_latest, btc_p, eth_p, cbeth_p

def get_explanation(z_score, z_points, coint_p=None, pair_name="BTC/ETH"):
    parts = pair_name.split("/")
    base  = parts[0] if len(parts) == 2 else "Base"
    quote = parts[1] if len(parts) == 2 else "Quote"

    coint_p_threshold = float(os.getenv("COINT_P_THRESHOLD", 0.25))
    coint_valid = coint_p is not None and coint_p < coint_p_threshold
    coint_unknown = coint_p is None

    def coint_suffix():
        if coint_unknown:
            return ""
        if coint_valid:
            return f" The {pair_name} pair is statistically cointegrated (p={coint_p:.4f}), so mean reversion signals are reliable."
        return (
            f" However, the cointegration check is currently failing (p={coint_p:.4f}, threshold {coint_p_threshold}), "
            f"meaning {base} and {quote} are not moving together in a statistically predictable way right now. "
            f"All trades are paused until cointegration is re-established."
        )

    if z_points < 20:
        return f"{pair_name} is in Observation Mode. More data points are needed to establish a reliable baseline before trading begins."

    if abs(z_score) < 1.0:
        msg = f"The {pair_name} ratio is near its rolling average. Standing by for a deviation of at least ±{Z_THRESHOLD} standard deviations."
        return msg + coint_suffix()

    if z_score > 0:
        msg = f"{base} is gaining value faster than {quote} relative to their recent average. "
        if z_score >= Z_THRESHOLD:
            if coint_valid or coint_unknown:
                msg += f"{base} is significantly overvalued — SELL signal detected (Mean Reversion)."
            else:
                msg += f"{base} is significantly overvalued and a SELL signal has been detected, but trading is blocked."
        else:
            msg += f"Watching for a potential peak to sell {base} / buy {quote}."
        return msg + coint_suffix()
    else:
        msg = f"{quote} is gaining value faster than {base} relative to their recent average. "
        if z_score <= -Z_THRESHOLD:
            if coint_valid or coint_unknown:
                msg += f"{base} is significantly undervalued — BUY signal detected (Mean Reversion)."
            else:
                msg += f"{base} is significantly undervalued and a BUY signal has been detected, but trading is blocked."
        else:
            msg += f"Watching for a potential dip to buy {base} / sell {quote}."
        return msg + coint_suffix()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    address = get_wallet_address()
    eth_bal, btc_bal, cbeth_bal = get_balances(address)
    logs, pairs_data, btc_p, eth_p, cbeth_p = parse_logs()

    coint_p_threshold = float(os.getenv("COINT_P_THRESHOLD", 0.25))

    pairs = []
    for pair_name, pdata in pairs_data.items():
        z = pdata["z_score"]
        z_pct = max(0, min(100, (z + 4) * 12.5))
        z_color = "var(--accent)" if (z > Z_THRESHOLD or z < -Z_THRESHOLD) else "var(--primary)"
        pairs.append({
            "name":           pair_name,
            "z_score":        z,
            "z_score_pct":    z_pct,
            "z_score_color":  z_color,
            "z_score_points": pdata.get("z_points", 0),
            "coint_p":        pdata.get("coint_p"),
            "explanation":    get_explanation(z, pdata.get("z_points", 0), pdata.get("coint_p"), pair_name),
        })

    if not pairs:
        pairs = [{
            "name": "BTC/ETH", "z_score": 0.0, "z_score_pct": 50,
            "z_score_color": "var(--primary)", "z_score_points": 0,
            "coint_p": None,
            "explanation": get_explanation(0.0, 0, None, "BTC/ETH"),
        }]

    perf = get_performance_metrics()
    if perf["open_position"] and btc_p > 0 and eth_p > 0:
        current_ratio = btc_p / eth_p
        entry_ratio = perf["open_position"]["entry_ratio"]
        if entry_ratio > 0:
            perf["unrealized_pnl"] = (current_ratio - entry_ratio) / entry_ratio * 100

    return templates.TemplateResponse("index.html", {
        "request":          request,
        "balance":          eth_bal,
        "cbbtc_balance":    btc_bal,
        "cbeth_balance":    cbeth_bal,
        "address":          address,
        "btc_price":        btc_p,
        "eth_price":        eth_p,
        "cbeth_price":      cbeth_p,
        "pairs":            pairs,
        "z_threshold":      Z_THRESHOLD,
        "coint_p_threshold": coint_p_threshold,
        "logs":             logs,
        "dry_run":          DRY_RUN,
        "perf":             perf,
    })

@app.get("/api/z-history")
async def z_history(pair: str = "BTC/ETH"):
    import pandas as pd
    price_history_file = os.getenv("PRICE_HISTORY_FILE", "price_history.json")
    if not os.path.exists(price_history_file):
        return JSONResponse([])
    try:
        with open(price_history_file) as f:
            raw = json.load(f)
    except Exception:
        return JSONResponse([])

    # Handle both multi-pair dict and legacy flat list
    if isinstance(raw, dict):
        records = raw.get(pair, [])
    else:
        records = raw  # legacy: always BTC/ETH

    if not records:
        return JSONResponse([])

    df = pd.DataFrame(records)
    # Support both old (btc_price/eth_price) and new (base_price/quote_price) column names
    if "base_price" in df.columns:
        df["_base"]  = df["base_price"].astype(float)
        df["_quote"] = df["quote_price"].astype(float)
    else:
        df["_base"]  = df["btc_price"].astype(float)
        df["_quote"] = df["eth_price"].astype(float)

    df["ratio"] = df["_base"] / df["_quote"]
    window = WINDOW_SIZE_HOURS * 12
    df["rm"] = df["ratio"].rolling(window, min_periods=2).mean()
    df["rs"] = df["ratio"].rolling(window, min_periods=2).std()
    df["z"]  = (df["ratio"] - df["rm"]) / df["rs"]

    result = [
        {
            "t":   row["timestamp"],
            "z":   round(float(row["z"]), 4),
            "btc": round(float(row["_base"]), 2),
            "eth": round(float(row["_quote"]), 2),
        }
        for _, row in df.iterrows()
        if pd.notna(row["z"])
    ]
    return JSONResponse(result[-288:])


@app.get("/api/trades")
async def trades_api():
    trades = load_jsonl(TRADES_FILE)
    return JSONResponse(list(reversed(trades[-30:])))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
