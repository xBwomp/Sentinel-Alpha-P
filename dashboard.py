import os
import json
import time
import requests
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
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

# Constants for Balance Checking
RPC_URLS = {
    "base-mainnet": "https://mainnet.base.org",
    "base-sepolia": "https://sepolia.base.org"
}
CBBTC_ADDRESSES = {
    "base-mainnet": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
    "base-sepolia": "0xcbB7C0006F23900c38EB856149F799620fcb8A4a"
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
    try:
        rpc_url = RPC_URLS.get(NETWORK_ID, RPC_URLS["base-mainnet"])
        w3 = Web3(Web3.HTTPProvider(rpc_url))

        if address and address != "Unknown":
            eth_balance = float(w3.eth.get_balance(address)) / 1e18

            cbbtc_addr = CBBTC_ADDRESSES.get(NETWORK_ID)
            if cbbtc_addr:
                contract = w3.eth.contract(address=cbbtc_addr, abi=ERC20_ABI)
                raw_balance = contract.functions.balanceOf(address).call()
                decimals = contract.functions.decimals().call()
                cbbtc_balance = float(raw_balance) / (10 ** decimals)
    except Exception as e:
        print(f"Error fetching balances: {e}")

    result = (eth_balance, cbbtc_balance)
    _balance_cache["data"] = result
    _balance_cache["ts"] = time.time()
    return result

def parse_logs():
    # Recent log lines for display
    recent_logs = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r') as f:
            lines = f.readlines()
        recent_logs = [l.strip() for l in lines[-50:]]
        recent_logs.reverse()

    # z_score and prices from structured trades.json
    z_score = 0.0
    btc_p = 0.0
    eth_p = 0.0
    trades = load_jsonl(TRADES_FILE)
    if trades:
        last = trades[-1]
        z_score = last.get("z_score", 0.0)
        btc_p = last.get("btc_price", 0.0)
        eth_p = last.get("eth_price", 0.0)

    # z_points from price_history.json row count
    z_points = 0
    price_history_file = os.getenv("PRICE_HISTORY_FILE", "price_history.json")
    if os.path.exists(price_history_file):
        try:
            with open(price_history_file) as f:
                z_points = len(json.load(f))
        except Exception:
            pass

    return recent_logs, z_score, z_points, btc_p, eth_p

def get_explanation(z_score, z_points):
    if z_points < 20:
        return "The bot is currently in 'Observation Mode'. It needs more data points to establish a reliable baseline for the BTC/ETH ratio before it can start identifying trading opportunities."
    
    if abs(z_score) < 1.0:
        return f"Market is currently balanced. The BTC/ETH ratio is near its 24-hour average. The bot is standing by, watching for a price deviation of at least {Z_THRESHOLD} standard deviations."
    
    if z_score > 0:
        msg = "BTC is gaining value faster than ETH relative to their recent average. "
        if z_score >= Z_THRESHOLD:
            return msg + "BTC is significantly overvalued. The bot has detected a SELL signal for BTC (Mean Reversion)."
        return msg + "The bot is watching for a potential peak to sell BTC/buy ETH."
    else:
        msg = "ETH is gaining value faster than BTC relative to their recent average. "
        if z_score <= -Z_THRESHOLD:
            return msg + "BTC is significantly undervalued. The bot has detected a BUY signal for BTC (Mean Reversion)."
        return msg + "The bot is watching for a potential dip to buy BTC/sell ETH."

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    address = get_wallet_address()
    eth_bal, btc_bal = get_balances(address)
    logs, z_score, z_points, btc_p, eth_p = parse_logs()

    z_pct = max(0, min(100, (z_score + 4) * 12.5))
    z_color = "var(--primary)"
    if z_score > Z_THRESHOLD or z_score < -Z_THRESHOLD:
        z_color = "var(--accent)"

    status_explanation = get_explanation(z_score, z_points)

    perf = get_performance_metrics()
    if perf["open_position"] and btc_p > 0 and eth_p > 0:
        current_ratio = btc_p / eth_p
        entry_ratio = perf["open_position"]["entry_ratio"]
        if entry_ratio > 0:
            perf["unrealized_pnl"] = (current_ratio - entry_ratio) / entry_ratio * 100

    return templates.TemplateResponse("index.html", {
        "request": request,
        "balance": eth_bal,
        "cbbtc_balance": btc_bal,
        "address": address,
        "btc_price": btc_p,
        "eth_price": eth_p,
        "z_score": z_score,
        "z_score_points": z_points,
        "z_score_pct": z_pct,
        "z_score_color": z_color,
        "z_threshold": Z_THRESHOLD,
        "logs": logs,
        "dry_run": DRY_RUN,
        "status_explanation": status_explanation,
        "perf": perf,
    })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
