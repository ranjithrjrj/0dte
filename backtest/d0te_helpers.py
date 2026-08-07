"""Shared DB helpers + result writers for the 0DTE backtest strategies."""
import csv as _csv
import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def at_ist(d, hm: str) -> datetime:
    """Return aware UTC datetime for the given IST clock time on date d."""
    hh, mm, ss = map(int, hm.split(":"))
    dt = datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=IST)
    return dt.astimezone(UTC)


def last_futures_price(cur, ts):
    cur.execute("SELECT price FROM futures_trades WHERE ts <= %s ORDER BY ts DESC LIMIT 1", (ts,))
    r = cur.fetchone()
    return float(r[0]) if r else None


def last_option_trade(cur, opt_type, strike, expiry, ts):
    cur.execute(
        "SELECT price FROM options_trades "
        "WHERE opt_type=%s AND strike=%s AND expiry=%s AND ts<=%s "
        "ORDER BY ts DESC LIMIT 1", (opt_type, strike, expiry, ts))
    r = cur.fetchone()
    return float(r[0]) if r else None


def last_option_trade_ts(cur, opt_type, strike, expiry, ts):
    cur.execute(
        "SELECT ts FROM options_trades "
        "WHERE opt_type=%s AND strike=%s AND expiry=%s AND ts<=%s "
        "ORDER BY ts DESC LIMIT 1", (opt_type, strike, expiry, ts))
    r = cur.fetchone()
    return r[0] if r else None


def min_option_price(cur, opt_type, strike, expiry, ts_lo, ts_hi):
    cur.execute(
        "SELECT min(price) FROM options_trades "
        "WHERE opt_type=%s AND strike=%s AND expiry=%s AND ts>=%s AND ts<=%s",
        (opt_type, strike, expiry, ts_lo, ts_hi))
    r = cur.fetchone()
    return float(r[0]) if r and r[0] is not None else None


def first_limit_trade_ts(cur, opt_type, strike, expiry, ts_lo, ts_hi, limit):
    cur.execute(
        "SELECT min(ts) FROM options_trades "
        "WHERE opt_type=%s AND strike=%s AND expiry=%s AND ts>=%s AND ts<=%s AND price<=%s",
        (opt_type, strike, expiry, ts_lo, ts_hi, limit))
    r = cur.fetchone()
    return r[0] if r and r[0] is not None else None


def first_trade_ts_price_ge(cur, opt_type, strike, expiry, ts_lo, ts_hi, px):
    """First trade in window where price >= px (for stop-loss triggers)."""
    cur.execute(
        "SELECT min(ts) FROM options_trades "
        "WHERE opt_type=%s AND strike=%s AND expiry=%s AND ts>=%s AND ts<=%s AND price>=%s",
        (opt_type, strike, expiry, ts_lo, ts_hi, px))
    r = cur.fetchone()
    return r[0] if r and r[0] is not None else None


def write_results(cfg, trades, equity, skipped):
    results_dir = Path(cfg.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    with open(results_dir / "trades.csv", "w", newline="", encoding="utf-8") as f:
        if trades:
            w = _csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            w.writeheader()
            w.writerows(trades)

    with open(results_dir / "equity.csv", "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=["date", "balance"])
        w.writeheader()
        w.writerows(equity)

    summary = summarize(cfg, trades, equity, skipped)
    with open(results_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    print("\nSummary written to", results_dir)
    return summary


def summarize(cfg, trades, equity, skipped):
    n = len(trades)
    wins = [t for t in trades if t["net_usd"] > 0]
    losses = [t for t in trades if t["net_usd"] < 0]
    gross_profit = sum(t["net_usd"] for t in wins)
    gross_loss = -sum(t["net_usd"] for t in losses)
    end_balance = equity[-1]["balance"] if equity else cfg.start_balance_usd
    total_pnl = end_balance - cfg.start_balance_usd
    return_pct = 100.0 * total_pnl / cfg.start_balance_usd

    peak = cfg.start_balance_usd
    max_dd = 0.0
    max_dd_pct = 0.0
    for e in equity:
        peak = max(peak, e["balance"])
        dd = peak - e["balance"]
        if dd > max_dd:
            max_dd = dd
        if peak > 0:
            dd_pct = 100.0 * dd / peak
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

    n_days = len(equity)
    years = n_days / 365.25
    cagr = (100.0 * ((end_balance / cfg.start_balance_usd) ** (1 / years) - 1)) if years > 0 and end_balance > 0 else 0.0

    return {
        "start_balance": cfg.start_balance_usd,
        "end_balance": round(end_balance, 2),
        "total_pnl_usd": round(total_pnl, 2),
        "total_return_pct": round(return_pct, 2),
        "cagr_pct": round(cagr, 2),
        "n_trades": n,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / n, 2) if n else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "avg_net_usd": round(total_pnl / n, 2) if n else 0.0,
        "total_fees_usd": round(sum(t["fees_usd"] for t in trades), 2),
        "gross_profit_usd": round(gross_profit, 2),
        "gross_loss_usd": round(gross_loss, 2),
        "max_drawdown_usd": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "best_day_usd": round(max((t["net_usd"] for t in trades), default=0), 2),
        "worst_day_usd": round(min((t["net_usd"] for t in trades), default=0), 2),
        "avg_lots": round(sum(t["lots"] for t in trades) / n, 2) if n else 0.0,
        "exit_breakdown": {
            et: sum(1 for t in trades if t["exit_type"] == et)
            for et in sorted({t["exit_type"] for t in trades})
        },
        "days_skipped": skipped,
        "date_from": equity[0]["date"] if equity else None,
        "date_to": equity[-1]["date"] if equity else None,
        "params": {k: v for k, v in cfg.__dict__.items()},
    }
