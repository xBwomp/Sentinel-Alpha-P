#!/usr/bin/env python3
"""
Sentinel Alpha Backtester

Replays price_history.json through the same signal and execution logic to
simulate strategy performance offline. Useful for validating parameter changes
(window, threshold, trade size) before going live.

Usage:
    python backtest.py
    python backtest.py --window 24 --threshold 2.0 --trade-size 0.10
    python backtest.py --adaptive --coint-gate
    python backtest.py --price-file custom_prices.json
"""

import json
import math
import argparse
from datetime import datetime, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# Strategy logic (mirrors main.py, no side effects)
# ---------------------------------------------------------------------------

def compute_z_score(ratios):
    if len(ratios) < 2:
        return None
    mean = sum(ratios) / len(ratios)
    variance = sum((r - mean) ** 2 for r in ratios) / (len(ratios) - 1)
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std == 0:
        return 0.0
    return (ratios[-1] - mean) / std


def adaptive_threshold(ratios, base, recent_n=12):
    """Scale base threshold by (recent vol / full-window vol). Clamped [0.5x, 2.0x]."""
    if len(ratios) < recent_n + 2:
        return base
    mean_full = sum(ratios) / len(ratios)
    full_std = math.sqrt(sum((r - mean_full) ** 2 for r in ratios) / (len(ratios) - 1))
    recent = ratios[-recent_n:]
    mean_r = sum(recent) / len(recent)
    recent_std = math.sqrt(sum((r - mean_r) ** 2 for r in recent) / (len(recent) - 1))
    if full_std == 0:
        return base
    return base * max(0.5, min(2.0, recent_std / full_std))


def check_cointegration(prices, min_points=20):
    """Return (is_valid, p_value). Fails open on errors."""
    if len(prices) < min_points:
        return False, None
    try:
        from statsmodels.tsa.stattools import coint
        btc = [p["btc_price"] for p in prices]
        eth = [p["eth_price"] for p in prices]
        _, p_value, _ = coint(btc, eth)
        return p_value < 0.05, p_value
    except Exception:
        return True, None


def scaled_trade_pct(z_score, threshold, base_pct, max_pct):
    excess = abs(z_score) - threshold
    scale = max(0.0, min(1.0, excess / threshold))
    return base_pct + scale * (max_pct - base_pct)


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------

def run_backtest(prices, window_hours=24, threshold=2.0, trade_size_pct=0.10,
                 max_trade_pct=0.40, cooldown_hours=1, daily_limit=50,
                 use_adaptive=False, use_coint_gate=False,
                 coint_recheck_interval=12):
    """
    Simulate strategy on a price series. Portfolio starts at 1.0 ETH, 0 cbBTC.
    Returns (sim_trades, final_portfolio_eth, strategy_return, btc_hold_return, eth_hold_return).
    """
    interval_minutes = 5
    window_size = int(window_hours * 60 / interval_minutes)
    cooldown = timedelta(hours=cooldown_hours)

    eth = 1.0
    cbbtc = 0.0
    last_trade_time = datetime.min
    trades_in_24h = []
    sim_trades = []

    # Cointegration cache
    coint_valid = False
    coint_last_check = 0

    for i in range(1, len(prices)):
        window = prices[max(0, i + 1 - window_size): i + 1]
        btc_price = prices[i]["btc_price"]
        eth_price = prices[i]["eth_price"]
        ts_raw = prices[i].get("timestamp")
        ts = datetime.fromisoformat(ts_raw) if ts_raw else datetime.min

        ratios = [p["btc_price"] / p["eth_price"] for p in window]

        # Cointegration gate
        if use_coint_gate:
            if i - coint_last_check >= coint_recheck_interval:
                coint_valid, _ = check_cointegration(window)
                coint_last_check = i
            if not coint_valid:
                continue

        z = compute_z_score(ratios)
        if z is None:
            continue

        active_threshold = adaptive_threshold(ratios, threshold) if use_adaptive else threshold

        # Expire old cooldown entries
        trades_in_24h = [t for t in trades_in_24h if ts - t < timedelta(days=1)]

        signal = None
        if (z < -active_threshold
                and len(trades_in_24h) < daily_limit
                and ts - last_trade_time >= cooldown):
            signal = "BUY"
        elif (z > active_threshold
              and len(trades_in_24h) < daily_limit
              and ts - last_trade_time >= cooldown):
            signal = "SELL"

        if signal:
            pct = scaled_trade_pct(z, active_threshold, trade_size_pct, max_trade_pct)
            if signal == "BUY" and eth > 0:
                amount_eth = eth * pct
                cbbtc += amount_eth * eth_price / btc_price
                eth -= amount_eth
                last_trade_time = ts
                trades_in_24h.append(ts)
                sim_trades.append({
                    "timestamp": ts.isoformat(),
                    "signal": "BUY",
                    "z_score": round(z, 6),
                    "btc_price": btc_price,
                    "eth_price": eth_price,
                    "ratio": round(btc_price / eth_price, 6),
                    "amount_eth": round(amount_eth, 8),
                    "threshold": round(active_threshold, 4),
                })
            elif signal == "SELL" and cbbtc > 0:
                amount_cbbtc = cbbtc * pct
                eth += amount_cbbtc * btc_price / eth_price
                cbbtc -= amount_cbbtc
                last_trade_time = ts
                trades_in_24h.append(ts)
                sim_trades.append({
                    "timestamp": ts.isoformat(),
                    "signal": "SELL",
                    "z_score": round(z, 6),
                    "btc_price": btc_price,
                    "eth_price": eth_price,
                    "ratio": round(btc_price / eth_price, 6),
                    "amount_cbbtc": round(amount_cbbtc, 8),
                    "threshold": round(active_threshold, 4),
                })

    # Final portfolio value in ETH
    last = prices[-1]
    final_portfolio_eth = eth + cbbtc * last["btc_price"] / last["eth_price"]
    first = prices[0]
    btc_hold_return = (last["btc_price"] - first["btc_price"]) / first["btc_price"]
    eth_hold_return = (last["eth_price"] - first["eth_price"]) / first["eth_price"]

    return sim_trades, final_portfolio_eth, final_portfolio_eth - 1.0, btc_hold_return, eth_hold_return


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def pair_trades(trades):
    pairs = []
    open_buy = None
    for t in trades:
        if t["signal"] == "BUY" and open_buy is None:
            open_buy = t
        elif t["signal"] == "SELL" and open_buy is not None:
            pairs.append({"open": open_buy, "close": t})
            open_buy = None
    return pairs, open_buy


def sep(char="=", width=54):
    return char * width


def main():
    parser = argparse.ArgumentParser(description="Sentinel Alpha Backtester")
    parser.add_argument("--price-file", default="price_history.json",
                        help="Price history JSON file (default: price_history.json)")
    parser.add_argument("--window", type=int, default=24,
                        help="Rolling window in hours (default: 24)")
    parser.add_argument("--threshold", type=float, default=2.0,
                        help="Z-score threshold (default: 2.0)")
    parser.add_argument("--trade-size", type=float, default=0.10, dest="trade_size",
                        help="Base trade size fraction (default: 0.10)")
    parser.add_argument("--max-trade-size", type=float, default=0.40, dest="max_trade_size",
                        help="Max trade size at extreme Z-scores (default: 0.40)")
    parser.add_argument("--adaptive", action="store_true",
                        help="Enable adaptive Z-score threshold")
    parser.add_argument("--coint-gate", action="store_true", dest="coint_gate",
                        help="Enable cointegration gate before trading")
    args = parser.parse_args()

    price_path = Path(args.price_file)
    if not price_path.exists():
        print(f"Error: {price_path} not found. Run the agent first to build price history.")
        return

    with open(price_path) as f:
        prices = json.load(f)

    if len(prices) < 10:
        print(f"Insufficient data: {len(prices)} points (need 10+).")
        return

    print(f"\nSentinel Alpha Backtester — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Price file    : {price_path} ({len(prices)} points)")
    first_ts = prices[0].get("timestamp", "?")[:16]
    last_ts = prices[-1].get("timestamp", "?")[:16]
    print(f"Date range    : {first_ts} → {last_ts}")
    print(f"Window        : {args.window}h")
    print(f"Threshold     : {args.threshold} {'(adaptive)' if args.adaptive else '(fixed)'}")
    print(f"Trade size    : {args.trade_size*100:.0f}%–{args.max_trade_size*100:.0f}% (scaled by Z)")
    print(f"Coint gate    : {'on' if args.coint_gate else 'off'}")
    print(sep())

    sim_trades, final_eth, strat_r, btc_r, eth_r = run_backtest(
        prices,
        window_hours=args.window,
        threshold=args.threshold,
        trade_size_pct=args.trade_size,
        max_trade_pct=args.max_trade_size,
        use_adaptive=args.adaptive,
        use_coint_gate=args.coint_gate,
    )

    buy_count = sum(1 for t in sim_trades if t["signal"] == "BUY")
    sell_count = sum(1 for t in sim_trades if t["signal"] == "SELL")
    pairs, open_pos = pair_trades(sim_trades)

    print(f"\n  Signals       : {buy_count} BUY, {sell_count} SELL ({len(sim_trades)} total)")
    print(f"  Paired trades : {len(pairs)} completed round trips")

    if pairs:
        returns = []
        for p in pairs:
            r_entry = p["open"]["ratio"]
            r_exit = p["close"]["ratio"]
            if r_entry > 0:
                returns.append((r_exit - r_entry) / r_entry)
        if returns:
            wins = sum(1 for r in returns if r > 0)
            print(f"  Win rate      : {wins/len(returns)*100:.1f}%  ({wins}/{len(returns)})")
            print(f"  Avg return    : {sum(returns)/len(returns)*100:+.3f}%")
            print(f"  Best trade    : {max(returns)*100:+.3f}%")
            print(f"  Worst trade   : {min(returns)*100:+.3f}%")

    if open_pos:
        print(f"  Open position : BUY at ratio {open_pos['ratio']:.4f} (unclosed at end of data)")

    print(f"\n  Portfolio Returns  (starting 1.0 ETH)")
    print(f"    Strategy     : {strat_r*100:+.3f}%  (final: {final_eth:.6f} ETH-equiv)")
    print(f"    BTC-hold     : {btc_r*100:+.3f}%")
    print(f"    ETH-hold     : {eth_r*100:+.3f}%")

    print(f"\n  Cointegration Test  (full dataset)")
    valid, p_val = check_cointegration(prices, min_points=20)
    if p_val is None:
        print(f"    Insufficient data (need 20+ points)")
    else:
        status = "COINTEGRATED" if valid else "NOT COINTEGRATED"
        flag = "  *** strategy basis may be invalid ***" if not valid else ""
        print(f"    p-value : {p_val:.4f}  —  {status}{flag}")

    print()


if __name__ == "__main__":
    main()
