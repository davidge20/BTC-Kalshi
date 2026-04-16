"""
Backtest entrypoint.

Run:
  export KALSHI_EDGE_CONFIG_JSON=/path/to/config.json
  python3 -m kalshi_edge.backtesting.backtest
"""

from __future__ import annotations

import faulthandler
faulthandler.enable()

import argparse
import os
from datetime import date, datetime, time, timedelta, timezone

from kalshi_edge.backtesting.backtest_engine import run_backtest
from kalshi_edge.backtesting.backtest_report import print_backtest_report
from kalshi_edge.strategy_config import (
    ENV_VAR as CONFIG_ENV_VAR,
    load_backtest_config,
    load_config,
)


def _resolve_interval_utc(start_date: str | None, end_date: str | None, days: int) -> tuple[datetime, datetime]:
    """
    Return [start, end) UTC.
    """
    if start_date and end_date:
        s = date.fromisoformat(start_date)
        e = date.fromisoformat(end_date)
        start_dt = datetime.combine(s, time(0, 0, 0), tzinfo=timezone.utc)
        end_dt = datetime.combine(e + timedelta(days=1), time(0, 0, 0), tzinfo=timezone.utc)
        return start_dt, end_dt

    today = datetime.now(timezone.utc).date()
    # Deterministic rolling window endpoint: start of current UTC day.
    end_dt = datetime.combine(today, time(0, 0, 0), tzinfo=timezone.utc)
    start_dt = end_dt - timedelta(days=int(days))
    return start_dt, end_dt


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="kalshi_edge.backtesting.backtest",
        description="Run minute-cadence backtest for Kalshi BTC ladder strategy.",
    )
    ap.add_argument(
        "--config",
        type=str,
        default=None,
        help="Optional override path for KALSHI_EDGE_CONFIG_JSON",
    )
    args = ap.parse_args()

    if args.config:
        os.environ[CONFIG_ENV_VAR] = str(args.config)

    cfg = load_config()
    bt = load_backtest_config()

    start_dt, end_dt = _resolve_interval_utc(bt.START_DATE, bt.END_DATE, bt.DAYS)
    print(
        "[backtest] resolved UTC interval: "
        f"{start_dt.isoformat().replace('+00:00', 'Z')} -> {end_dt.isoformat().replace('+00:00', 'Z')}"
    )

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    os.makedirs(bt.LOG_DIR, exist_ok=True)
    log_path = os.path.join(
        bt.LOG_DIR,
        f"backtest_{bt.SERIES_TICKER}_{start_dt.strftime('%Y%m%d')}_{(end_dt - timedelta(days=1)).strftime('%Y%m%d')}_{ts}.jsonl",
    )
    print(f"[backtest] log path: {log_path}")
    print("[backtest] vol model: regression (DVOL+RV) > GARCH(1,1) > trailing RV")
    print("[backtest] fill model: taker-only (entries at ask, exits at bid/settlement)")
    step_seconds = int(bt.STEP_SECONDS) if bt.STEP_SECONDS is not None else int(bt.STEP_MINUTES) * 60
    print(f"[backtest] cadence: every {step_seconds}s")
    print(f"[backtest] realized vol window: {int(bt.REALIZED_VOL_WINDOW_MINUTES)} minute(s)")
    if str(bt.POSITION_SIZING_MODE) == "kelly":
        print(
            f"[backtest] sizing: Kelly x {float(bt.KELLY_FRACTION):.2f} "
            f"on starting bankroll ${float(bt.STARTING_BANKROLL_DOLLARS):.2f}"
        )
    else:
        print(f"[backtest] sizing: fixed order size ({int(cfg.ORDER_SIZE)} contract step)")
    print("[backtest] note: MIN_TOP_SIZE gate is ignored (candles do not include depth/top-size).")
    if step_seconds < 60:
        print("[backtest] note: sub-minute checks reuse the latest available 1m candle until the next candle prints.")

    try:
        from kalshi_edge.http_client import HttpClient
    except ModuleNotFoundError as e:
        if getattr(e, "name", None) == "requests":
            raise SystemExit(
                "Missing dependency.\n"
                "Activate your environment and install requirements:\n"
                "  python3 -m pip install -r requirements.txt\n"
                "(missing: requests)"
            ) from e
        raise

    http = HttpClient(debug=bool(bt.DEBUG_HTTP))
    summary = run_backtest(http=http, cfg=cfg, bt=bt, start_dt=start_dt, end_dt=end_dt, log_path=log_path)
    print_backtest_report(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
