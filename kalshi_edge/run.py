#!/usr/bin/env python3
"""
run.py
"""

import argparse
import os
from time import sleep

from kalshi_edge.market_discovery import discover_current_event
from kalshi_edge.pipeline import evaluate_event
from kalshi_edge.render import render_once
from kalshi_edge.http_client import HttpClient
from kalshi_edge.constants import KALSHI


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", type=str, default=None)
    ap.add_argument("--url", type=str, default=None)
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--refresh-seconds", type=int, default=10)
    ap.add_argument("--window-minutes", type=int, default=70)
    ap.add_argument("--max-strikes", type=int, default=120)
    ap.add_argument("--band-pct", type=float, default=25.0)
    ap.add_argument("--sort", type=str, default="ev", choices=["sens", "strike", "ev"])
    ap.add_argument("--fee-cents", type=int, default=1)
    ap.add_argument("--depth-window-cents", type=int, default=2)
    ap.add_argument("--threads", type=int, default=10)
    ap.add_argument("--iv-band-pct", type=float, default=3.0)
    ap.add_argument("--debug-http", action="store_true")

    ap.add_argument("--trade", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--trade-count", type=int, default=1)
    ap.add_argument("--max-contracts", type=int, default=None)
    ap.add_argument("--min-minutes-left", type=float, default=2.0)

    ap.add_argument(
        "--state-file",
        type=str,
        default=os.environ.get("KALSHI_EDGE_STATE_FILE", ".kalshi_edge_state.json"),
    )
    ap.add_argument(
        "--trade-log-file",
        type=str,
        default=os.environ.get("KALSHI_EDGE_TRADE_LOG_FILE", "trade_log.jsonl"),
        help="Append-only JSONL log of trade actions + shutdown PnL snapshot.",
    )
    ap.add_argument("--reconcile-state", action="store_true")

    ap.add_argument("--api-key-id", type=str, default=os.environ.get("KALSHI_API_KEY_ID"))
    ap.add_argument("--private-key-path", type=str, default=os.environ.get("KALSHI_PRIVATE_KEY_PATH"))
    ap.add_argument("--kalshi-base-url", type=str, default=os.environ.get("KALSHI_BASE_URL", KALSHI))

    args = ap.parse_args()

    trader = None
    if args.trade:
        from kalshi_edge.kalshi_auth import KalshiAuth
        from kalshi_edge.trader_v1 import V1Trader

        if not args.api_key_id or not args.private_key_path:
            raise SystemExit("--trade requires --api-key-id and --private-key-path (or env vars)")

        http_trade = HttpClient(debug=args.debug_http)
        auth = KalshiAuth(api_key_id=args.api_key_id, private_key_path=args.private_key_path)

        max_contracts = args.max_contracts
        if max_contracts is None:
            max_contracts = args.trade_count

        trader = V1Trader(
            http=http_trade,
            auth=auth,
            kalshi_base_url=args.kalshi_base_url,
            state_file=args.state_file,
            trade_log_file=args.trade_log_file,
            fee_cents=args.fee_cents,
            count=args.trade_count,
            max_contracts=max_contracts,
            min_minutes_left_entry=args.min_minutes_left,
            min_edge_pp=0.05,
            capture_frac=0.70,
            stop_frac=0.50,
            min_stop_pp=0.06,
            exit_minutes_left=3.0,
            enable_edge_flip_exit=True,
            edge_flip_pp=0.02,
            dry_run=args.dry_run,
        )

    state_reconciled = False
    last_result = None

    def run_once() -> None:
        nonlocal state_reconciled, last_result

        event = args.event
        url = args.url

        if not event and not url:
            discovered = discover_current_event(window_minutes=args.window_minutes, debug_http=args.debug_http)
            event = discovered.event_ticker

        result = evaluate_event(
            event=event,
            url=url,
            max_strikes=args.max_strikes,
            band_pct=args.band_pct,
            sort=args.sort,
            fee_cents=args.fee_cents,
            depth_window_cents=args.depth_window_cents,
            threads=args.threads,
            iv_band_pct=args.iv_band_pct,
            debug_http=args.debug_http,
        )
        last_result = result

        if trader is not None and args.reconcile_state and not state_reconciled:
            trader.reconcile_state(result.event_ticker)
            state_reconciled = True

        render_once(
            result,
            sort_mode=args.sort,
            compact=args.watch,
            show_explainer=not args.watch,
            fit_to_terminal=args.watch,
            clear_screen=args.watch,
        )

        if trader is not None:
            trader.on_tick(result)

    try:
        if args.watch:
            while True:
                run_once()
                sleep(args.refresh_seconds)
        else:
            run_once()
    except KeyboardInterrupt:
        print("\n[RUN] Ctrl-C received, shutting down gracefully...")
        if trader is not None:
            try:
                trader.on_shutdown(last_result)
                print(f"[RUN] wrote shutdown snapshot to {args.trade_log_file}")
            except Exception as e:
                print(f"[RUN] failed to write shutdown snapshot: {e}")


if __name__ == "__main__":
    main()
