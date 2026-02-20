#!/usr/bin/env python3
"""
run.py
"""

import argparse
import os
import sys
from time import sleep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", type=str, default=None)
    ap.add_argument("--url", type=str, default=None)
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--refresh-seconds", type=int, default=10)
    ap.add_argument("--window-minutes", type=int, default=70)
    ap.add_argument("--max-strikes", type=int, default=120)
    ap.add_argument("--band-pct", type=float, default=25.0)
    ap.add_argument("--sort", type=str, default="ev", choices=["sens", "strike", "ev"])
    # Back-compat: ladder evaluation fee defaults to taker fee.
    ap.add_argument("--fee-cents", type=int, default=1, help="(legacy) fee used for ladder evaluation; defaults to 1")
    ap.add_argument("--fee-cents-maker", type=int, default=None)
    ap.add_argument("--fee-cents-taker", type=int, default=None)
    ap.add_argument("--depth-window-cents", type=int, default=2)
    ap.add_argument("--threads", type=int, default=10)
    ap.add_argument("--iv-band-pct", type=float, default=3.0)
    ap.add_argument("--debug-http", action="store_true")

    ap.add_argument("--trade", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--trade-count", type=int, default=1)
    ap.add_argument("--max-contracts", type=int, default=1_000_000)
    ap.add_argument("--min-minutes-left", type=float, default=2.0)
    ap.add_argument("--trader", type=str, default="v2", choices=["v1", "v2"])

    ap.add_argument("--order-mode", type=str, default="hybrid", choices=["taker_only", "maker_only", "hybrid"])
    ap.add_argument("--post-only", dest="post_only", action="store_true", default=True)
    ap.add_argument("--no-post-only", dest="post_only", action="store_false")
    ap.add_argument("--max-contracts-per-market", type=int, default=1)
    ap.add_argument("--order-refresh-seconds", type=int, default=10)
    ap.add_argument("--cancel-stale-seconds", type=int, default=60)
    ap.add_argument("--p-requote-pp", type=float, default=0.02)
    ap.add_argument("--max-entries-per-tick", type=int, default=1)

    ap.add_argument("--lock-event", dest="lock_event", action="store_true", default=True)
    ap.add_argument("--no-lock-event", dest="lock_event", action="store_false")
    ap.add_argument("--debug-order-manager", action="store_true", help="Run a tiny local simulation and exit.")

    ap.add_argument(
        "--state-file",
        type=str,
        default=os.environ.get("KALSHI_EDGE_STATE_FILE", ".kalshi_edge_state.json"),
    )
    ap.add_argument(
        "--trade-log-file",
        type=str,
        default=os.environ.get("KALSHI_EDGE_TRADE_LOG_FILE", "trade_log.jsonl"),
        help="Append-only JSONL log of trade actions + shutdown snapshot.",
    )
    ap.add_argument("--reconcile-state", action="store_true")

    ap.add_argument("--api-key-id", type=str, default=os.environ.get("KALSHI_API_KEY_ID"))
    ap.add_argument("--private-key-path", type=str, default=os.environ.get("KALSHI_PRIVATE_KEY_PATH"))
    ap.add_argument("--kalshi-base-url", type=str, default=os.environ.get("KALSHI_BASE_URL"))

    args = ap.parse_args()

    # Strategy config (Workflow B). Load early so config errors fail fast.
    from kalshi_edge.strategy_config import ENV_VAR as CONFIG_ENV_VAR, load_config

    config_path = os.environ.get(CONFIG_ENV_VAR)
    try:
        cfg = load_config()
    except ValueError as e:
        raise SystemExit(str(e))

    # Keep legacy CLI behavior when no JSON override is provided.
    if not config_path:
        cfg.FEE_CENTS = int(args.fee_cents)
        cfg.ORDER_SIZE = int(args.trade_count)
        cfg.validate()

    if config_path:
        print(f"[config] loaded {CONFIG_ENV_VAR}={config_path}", flush=True)
    else:
        print("[config] loaded defaults", flush=True)
    print(
        f"[config] MIN_EV={cfg.MIN_EV} ORDER_SIZE={cfg.ORDER_SIZE} MAX_COST_PER_EVENT={cfg.MAX_COST_PER_EVENT} "
        f"DEDUPE_MARKETS={cfg.DEDUPE_MARKETS} ALLOW_SCALE_IN={cfg.ALLOW_SCALE_IN} "
        f"MAX_CONTRACTS_PER_MARKET={cfg.MAX_CONTRACTS_PER_MARKET}",
        flush=True,
    )

    try:
        from kalshi_edge.constants import KALSHI
        from kalshi_edge.http_client import HttpClient
        from kalshi_edge.market_discovery import discover_current_event
        from kalshi_edge.pipeline import evaluate_event
        from kalshi_edge.render import render_once
    except ModuleNotFoundError as e:
        # Common issue in fresh envs: third-party deps not installed yet.
        if getattr(e, "name", None) in {"requests", "cryptography"}:
            raise SystemExit(
                "Missing dependency.\n"
                "Activate your environment and install requirements:\n"
                "  python3 -m pip install -r requirements.txt\n"
                f"(missing: {e.name})"
            ) from e
        raise

    if not args.kalshi_base_url:
        args.kalshi_base_url = KALSHI

    # Debug/sanity mode (no API keys needed)
    if args.debug_order_manager:
        from kalshi_edge.trader_v2 import debug_order_manager

        debug_order_manager()
        return

    # Fees: if explicit maker/taker not provided, fall back to --fee-cents.
    if args.fee_cents_maker is None:
        args.fee_cents_maker = int(args.fee_cents)
    if args.fee_cents_taker is None:
        args.fee_cents_taker = int(args.fee_cents)

    trader = None
    if args.trade:
        from kalshi_edge.kalshi_auth import KalshiAuth
        from kalshi_edge.trader_v1 import V1Trader
        from kalshi_edge.trader_v2 import V2Trader

        if not args.api_key_id or not args.private_key_path:
            raise SystemExit("--trade requires --api-key-id and --private-key-path (or env vars)")

        http_trade = HttpClient(debug=args.debug_http)
        auth = KalshiAuth(api_key_id=args.api_key_id, private_key_path=args.private_key_path)

        if args.trader == "v1":
            trader = V1Trader(
                http=http_trade,
                auth=auth,
                kalshi_base_url=args.kalshi_base_url,
                state_file=args.state_file,
                trade_log_file=args.trade_log_file,
                fee_cents=int(args.fee_cents_taker),
                count=args.trade_count,
                dry_run=args.dry_run,
            )
        else:
            trader = V2Trader(
                http=http_trade,
                auth=auth,
                kalshi_base_url=args.kalshi_base_url,
                state_file=args.state_file,
                trade_log_file=args.trade_log_file,
                dry_run=args.dry_run,
                config=cfg,
            )

    state_reconciled = False
    last_result = None
    locked_event = None

    def run_once() -> None:
        nonlocal state_reconciled, last_result, locked_event

        event = args.event
        url = args.url

        if not event and not url:
            if args.watch and sys.stdout.isatty():
                print("[RUN] discovering current Kalshi event...", flush=True)
            if args.lock_event and locked_event is not None:
                event = locked_event
            else:
                discovered = discover_current_event(window_minutes=args.window_minutes, debug_http=args.debug_http)
                event = discovered.event_ticker
                if args.lock_event:
                    locked_event = event

        if args.watch and sys.stdout.isatty():
            print("[RUN] fetching prices + evaluating ladder (this can take a bit)...", flush=True)

        result = evaluate_event(
            event=event,
            url=url,
            max_strikes=args.max_strikes,
            band_pct=args.band_pct,
            sort=args.sort,
            fee_cents=int(cfg.FEE_CENTS),
            depth_window_cents=args.depth_window_cents,
            threads=args.threads,
            iv_band_pct=args.iv_band_pct,
            debug_http=args.debug_http,
        )
        last_result = result

        # If we are locking to one event, release the lock after it closes (or is effectively closed).
        if args.lock_event and locked_event is not None:
            if float(result.minutes_left) <= float(args.min_minutes_left):
                locked_event = None

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
