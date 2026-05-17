#!/usr/bin/env python3
"""
Sentinel Alpha Performance Analyzer

Usage:
    python analyze.py              # weekly report (last 7 days)
    python analyze.py --days 14    # last 14 days
    python analyze.py --monthly    # include monthly report + cointegration test
"""

import json
import math
import argparse
from datetime import datetime, timedelta
from pathlib import Path

TRADES_FILE = "trades.json"
DAILY_SUMMARY_FILE = "daily_summary.json"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_jsonl(path):
    records = []
    p = Path(path)
    if not p.exists():
        return records
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


# ---------------------------------------------------------------------------
# Trade pairing
# ---------------------------------------------------------------------------

def pair_trades(trades):
    """
    Pair BUY→SELL sequences into round trips, grouped by trading pair.
    Returns (pairs, open_positions) where:
      - pairs is a list of {'pair': str, 'open': trade_record, 'close': trade_record}
      - open_positions is a dict of pair_name -> open BUY trade record
    """
    by_pair = {}
    for t in sorted(trades, key=lambda x: x["timestamp"]):
        name = t.get("pair", "unknown")
        if name not in by_pair:
            by_pair[name] = {"open": None, "pairs": []}
        if t["signal"] == "BUY" and by_pair[name]["open"] is None:
            by_pair[name]["open"] = t
        elif t["signal"] == "SELL" and by_pair[name]["open"] is not None:
            by_pair[name]["pairs"].append({"pair": name, "open": by_pair[name]["open"], "close": t})
            by_pair[name]["open"] = None

    pairs = [p for group in by_pair.values() for p in group["pairs"]]
    open_positions = {name: group["open"] for name, group in by_pair.items() if group["open"] is not None}
    return pairs, open_positions


def pair_return(pair):
    """
    Return for a BUY→SELL pair.
    The strategy bets the BTC/ETH ratio will revert upward after a BUY signal,
    so profit = (ratio_at_close - ratio_at_open) / ratio_at_open.
    """
    r_entry = pair["open"]["ratio"]
    r_exit = pair["close"]["ratio"]
    if r_entry == 0:
        return None
    return (r_exit - r_entry) / r_entry


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------

def _sep(char="=", width=54):
    return char * width


def _pct(val):
    return f"{val*100:+.3f}%"


def _daily_returns(summaries):
    """Compute day-over-day portfolio returns from daily summaries."""
    vals = [
        s["portfolio_eth_value_eod"]
        for s in sorted(summaries, key=lambda x: x["date"])
        if "portfolio_eth_value_eod" in s and s["portfolio_eth_value_eod"] > 0
    ]
    if len(vals) < 2:
        return []
    return [(vals[i] - vals[i - 1]) / vals[i - 1] for i in range(1, len(vals))]


def _sharpe(returns, periods_per_year=365):
    if len(returns) < 2:
        return None
    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std_r = math.sqrt(variance)
    if std_r == 0:
        return None
    return (mean_r / std_r) * math.sqrt(periods_per_year)


def _max_drawdown(summaries):
    vals = [
        s["portfolio_eth_value_eod"]
        for s in sorted(summaries, key=lambda x: x["date"])
        if "portfolio_eth_value_eod" in s
    ]
    if not vals:
        return None
    peak = vals[0]
    max_dd = 0.0
    for v in vals:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return max_dd


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def weekly_report(trades, summaries, days=7):
    cutoff_dt = (datetime.now() - timedelta(days=days)).isoformat()
    cutoff_date = cutoff_dt[:10]

    rt = [t for t in trades if t["timestamp"] >= cutoff_dt]
    rs = [s for s in summaries if s["date"] >= cutoff_date]

    print(f"\n{_sep()}")
    print(f"  WEEKLY REPORT  (last {days} days — {cutoff_date} to today)")
    print(_sep())

    # --- Activity ---
    buy_count = sum(1 for t in rt if t["signal"] == "BUY")
    sell_count = sum(1 for t in rt if t["signal"] == "SELL")
    ignored_cd = sum(s.get("signals_ignored_cooldown", 0) for s in rs)
    ignored_lim = sum(s.get("signals_ignored_limit", 0) for s in rs)

    print(f"\n  Trade Activity")
    print(f"    BUY signals fired  : {buy_count}")
    print(f"    SELL signals fired : {sell_count}")
    print(f"    Ignored (cooldown) : {ignored_cd}")
    print(f"    Ignored (cap)      : {ignored_lim}")

    # --- Round-trip performance ---
    pairs, open_positions = pair_trades(rt)
    print(f"\n  Round-Trip Performance  ({len(pairs)} completed pairs)")
    if pairs:
        returns = [r for r in (pair_return(p) for p in pairs) if r is not None]
        wins = sum(1 for r in returns if r > 0)
        win_rate = wins / len(returns) if returns else 0
        avg_r = sum(returns) / len(returns) if returns else 0

        print(f"    Win rate    : {win_rate*100:.1f}%  ({wins}/{len(returns)})")
        print(f"    Avg return  : {_pct(avg_r)}")
        if returns:
            print(f"    Best trade  : {_pct(max(returns))}")
            print(f"    Worst trade : {_pct(min(returns))}")

        daily_r = _daily_returns(rs)
        sharpe = _sharpe(daily_r)
        if sharpe is not None:
            print(f"    Sharpe (ann): {sharpe:.2f}  (based on {len(daily_r)} daily returns)")
    else:
        print("    No completed round trips yet.")

    if open_positions:
        for pair_name, open_pos in open_positions.items():
            age = datetime.now() - datetime.fromisoformat(open_pos["timestamp"])
            print(
                f"\n  Open Position [{pair_name}]: BUY at ratio {open_pos['ratio']:.4f} "
                f"— {int(age.total_seconds()//3600)}h ago"
            )

    # --- Portfolio value ---
    if rs:
        first = sorted(rs, key=lambda x: x["date"])[0]
        last = sorted(rs, key=lambda x: x["date"])[-1]
        start_val = first.get("portfolio_eth_value_eod", 0)
        end_val = last.get("portfolio_eth_value_eod", 0)
        print(f"\n  Portfolio (ETH-equiv)")
        print(f"    Start  : {start_val:.6f} ETH  ({first['date']})")
        print(f"    End    : {end_val:.6f} ETH  ({last['date']})")
        if start_val > 0:
            print(f"    Return : {_pct((end_val - start_val) / start_val)}")

    # --- Z-score distribution ---
    if rs:
        z_mins = [s["z_score_min"] for s in rs if "z_score_min" in s]
        z_maxs = [s["z_score_max"] for s in rs if "z_score_max" in s]
        z_means = [s["z_score_mean"] for s in rs if "z_score_mean" in s]
        if z_mins:
            avg_mean = sum(z_means) / len(z_means)
            print(f"\n  Z-Score Distribution")
            print(f"    Range    : [{min(z_mins):.3f}, {max(z_maxs):.3f}]")
            print(f"    Avg mean : {avg_mean:.3f}")
            threshold_hits = buy_count + sell_count
            total_periods = len(z_mins) * (24 * 60 // 5)  # rough: readings per day
            if total_periods > 0:
                hit_rate = threshold_hits / (total_periods if total_periods else 1) * 100
                print(f"    Signal rate : ~{buy_count + sell_count} signals over {len(z_mins)} days")


def monthly_report(trades, summaries):
    cutoff_dt = (datetime.now() - timedelta(days=30)).isoformat()
    cutoff_date = cutoff_dt[:10]

    rt = [t for t in trades if t["timestamp"] >= cutoff_dt]
    rs = [s for s in summaries if s["date"] >= cutoff_date]

    print(f"\n{_sep()}")
    print(f"  MONTHLY REPORT  (last 30 days — {cutoff_date} to today)")
    print(_sep())

    # --- Max drawdown ---
    max_dd = _max_drawdown(rs)
    print(f"\n  Risk Metrics")
    if max_dd is not None:
        print(f"    Max drawdown : {max_dd*100:.2f}%")
    else:
        print("    Max drawdown : insufficient data")

    # --- Benchmark comparison ---
    if rt:
        first = sorted(rt, key=lambda x: x["timestamp"])[0]
        last = sorted(rt, key=lambda x: x["timestamp"])[-1]
        btc_r = (last["btc_price"] - first["btc_price"]) / first["btc_price"]
        eth_r = (last["eth_price"] - first["eth_price"]) / first["eth_price"]

        # Strategy portfolio return over same window
        rs_sorted = sorted(rs, key=lambda x: x["date"])
        strat_r = None
        if len(rs_sorted) >= 2:
            sv = rs_sorted[0].get("portfolio_eth_value_eod", 0)
            ev = rs_sorted[-1].get("portfolio_eth_value_eod", 0)
            strat_r = (ev - sv) / sv if sv > 0 else None

        print(f"\n  Benchmark Comparison  ({first['timestamp'][:10]} → {last['timestamp'][:10]})")
        print(f"    BTC price change  : {_pct(btc_r)}")
        print(f"    ETH price change  : {_pct(eth_r)}")
        if strat_r is not None:
            print(f"    Strategy (ETH-eq) : {_pct(strat_r)}")
            edge_vs_btc = strat_r - btc_r
            edge_vs_eth = strat_r - eth_r
            print(f"    Edge vs BTC-hold  : {_pct(edge_vs_btc)}")
            print(f"    Edge vs ETH-hold  : {_pct(edge_vs_eth)}")
    else:
        print("\n  Benchmark: no trade records in last 30 days")

    # --- Cointegration test ---
    print(f"\n  Cointegration Test  (BTC/ETH, last 30 days of trade records)")
    btc_prices = [t["btc_price"] for t in rt]
    eth_prices = [t["eth_price"] for t in rt]

    if len(btc_prices) < 20:
        print(f"    Insufficient data ({len(btc_prices)} points, need 20+). Run longer first.")
    else:
        try:
            from statsmodels.tsa.stattools import coint
            _, p_value, _ = coint(btc_prices, eth_prices)
            status = "COINTEGRATED" if p_value < 0.05 else "NOT COINTEGRATED"
            flag = ""
            if p_value >= 0.05:
                flag = "  *** STRATEGY BASIS MAY BE INVALID ***"
            print(f"    p-value : {p_value:.4f}  —  {status}{flag}")
            print(f"    (Strategy requires p < 0.05 for valid mean-reversion signals)")
        except ImportError:
            print("    statsmodels not installed — run: pip install statsmodels")
        except Exception as e:
            print(f"    Error running test: {e}")

    # --- All-time pair performance ---
    all_pairs, _ = pair_trades(trades)
    if all_pairs:
        print(f"\n  All-Time Stats  ({len(all_pairs)} completed round trips)")
        by_pair_name = {}
        for p in all_pairs:
            by_pair_name.setdefault(p["pair"], []).append(p)
        for pair_name, group in sorted(by_pair_name.items()):
            returns = [r for r in (pair_return(p) for p in group) if r is not None]
            if not returns:
                continue
            wins = sum(1 for r in returns if r > 0)
            print(f"    [{pair_name}]  {len(returns)} trips  |  "
                  f"win rate {wins/len(returns)*100:.1f}%  |  "
                  f"avg {_pct(sum(returns)/len(returns))}  |  "
                  f"best {_pct(max(returns))}  worst {_pct(min(returns))}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sentinel Alpha Performance Analyzer")
    parser.add_argument("--days", type=int, default=7, help="Window for weekly report (default: 7)")
    parser.add_argument("--monthly", action="store_true", help="Include 30-day report + cointegration test")
    parser.add_argument("--trades-file", default=TRADES_FILE, dest="trades_file")
    parser.add_argument("--summary-file", default=DAILY_SUMMARY_FILE, dest="summary_file")
    args = parser.parse_args()

    trades = load_jsonl(args.trades_file)
    summaries = load_jsonl(args.summary_file)

    print(f"\nSentinel Alpha Analyzer — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Loaded {len(trades)} trade records, {len(summaries)} daily summaries")

    if not trades and not summaries:
        print("\nNo data yet. Run the agent first to accumulate trade records.")
        return

    weekly_report(trades, summaries, days=args.days)

    if args.monthly:
        monthly_report(trades, summaries)

    print()


if __name__ == "__main__":
    main()
