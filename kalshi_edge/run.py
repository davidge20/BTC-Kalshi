#!/usr/bin/env python3
"""
run.py — CLI entrypoint for kalshi_edge.

Also invocable as ``python -m kalshi_edge`` (via __main__.py).

All strategy/tuning parameters live in strategy_config.json.
The CLI only exposes operational flags (what to do, credentials, debug).

Usage:
  python -m kalshi_edge --config strategy_config.json --watch
  python -m kalshi_edge --config strategy_config.json --watch --trade --dry-run
  python -m kalshi_edge --config strategy_config.json --event KXBTCD-25FEB2108 --trade
"""

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone
from time import sleep


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="kalshi_edge",
        description="Evaluate + trade Kalshi BTC ladder markets against a probability model.",
    )

    # --- target selection ---
    ap.add_argument("--event", type=str, default=None, help="Kalshi event ticker (e.g. KXBTCD-25FEB2108)")
    ap.add_argument("--url", type=str, default=None, help="Kalshi event URL (alternative to --event)")

    # --- runtime mode ---
    ap.add_argument("--config", type=str, default=None,
                    help="Path to strategy_config.json (alternative to KALSHI_EDGE_CONFIG_JSON env var)")
    ap.add_argument("--watch", action="store_true", help="Continuous loop mode")
    ap.add_argument("--trade", action="store_true", help="Enable trading")
    ap.add_argument("--dry-run", action="store_true", help="Paper trading (no real orders)")
    ap.add_argument("--reconcile-state", action="store_true", help="Reconcile position state on startup")

    # --- credentials (env vars preferred) ---
    ap.add_argument("--api-key-id", type=str, default=os.environ.get("KALSHI_API_KEY_ID"))
    ap.add_argument("--private-key-path", type=str, default=os.environ.get("KALSHI_PRIVATE_KEY_PATH"))
    ap.add_argument("--kalshi-base-url", type=str, default=os.environ.get("KALSHI_BASE_URL"))
    ap.add_argument("--subaccount", type=int, default=None)

    # --- path overrides ---
    ap.add_argument("--state-file", type=str, default=None, help="Override state file path")
    ap.add_argument("--trade-log-file", type=str, default=None,
                    help="Override trade log file path (disables per-run log dir)")

    # --- debug / test ---
    ap.add_argument("--debug-http", action="store_true")
    ap.add_argument("--debug-order-manager", action="store_true", help="Run a tiny local simulation and exit")
    ap.add_argument("--strict-log-schema", action="store_true", help="Raise on missing required log fields")

    args = ap.parse_args()

    # --- Load config ---
    from kalshi_edge.strategy_config import ENV_VAR as CONFIG_ENV_VAR, config_hash, config_source_path, config_to_dict, load_config
    from kalshi_edge.trade_log import resolve_trade_log_path

    if args.config:
        os.environ[CONFIG_ENV_VAR] = args.config

    config_path = os.environ.get(CONFIG_ENV_VAR)
    try:
        cfg = load_config()
    except ValueError as e:
        raise SystemExit(str(e))

    run_id = str(uuid.uuid4())
    cfg_hash = config_hash(cfg)
    cfg_path = config_source_path()

    # --- Resolve log / state paths ---
    state_file = args.state_file or os.environ.get("KALSHI_EDGE_STATE_FILE", ".kalshi_edge_state.json")

    trade_log_file = args.trade_log_file or os.environ.get("KALSHI_EDGE_TRADE_LOG_FILE", "trade_log.jsonl")
    trade_log_dir = None if args.trade_log_file is not None else (cfg.TRADE_LOG_DIR or None)
    if cfg.TRADE_LOG_DIR and args.trade_log_file is not None:
        print("[log] ignoring config TRADE_LOG_DIR because --trade-log-file was explicitly set", flush=True)
    trade_log_path = resolve_trade_log_path(
        trade_log_file=str(trade_log_file),
        trade_log_dir=str(trade_log_dir) if trade_log_dir else None,
        run_id=run_id,
        now_utc=datetime.now(timezone.utc),
    )
    print(f"[log] trade log -> {trade_log_path}", flush=True)

    if args.state_file is None and trade_log_dir:
        dt = datetime.now(timezone.utc)
        day = dt.strftime("%Y-%m-%d")
        base_dir = str(trade_log_dir).rstrip("/").rstrip("\\")
        logs_root = os.path.dirname(base_dir) or base_dir
        state_file = os.path.join(logs_root, "state", day, f"{run_id}.json")
    print(f"[log] state file -> {state_file}", flush=True)

    # --- Write per-run config snapshot ---
    def _write_run_config(trade_log_path: str, cfg_dict: dict, run_id: str) -> None:
        import json as _json
        log_dir = os.path.dirname(os.path.abspath(trade_log_path))
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            cfg_out = os.path.join(log_dir, f"{run_id}.config.json")
            try:
                with open(cfg_out, "w", encoding="utf-8") as _f:
                    _json.dump(cfg_dict, _f, indent=2, sort_keys=True)
                print(f"[log] run config -> {cfg_out}", flush=True)
            except Exception as _e:
                print(f"[log] failed to write run config: {_e}", flush=True)

    _write_run_config(trade_log_path, config_to_dict(cfg), run_id)

    from kalshi_edge.util.git import best_effort_git_commit

    git_commit = best_effort_git_commit(start_paths=[os.getcwd(), os.path.dirname(__file__)])

    if config_path:
        print(f"[config] loaded {CONFIG_ENV_VAR}={config_path}", flush=True)
    else:
        print("[config] loaded defaults (no config file — set --config or KALSHI_EDGE_CONFIG_JSON)", flush=True)
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

    if args.debug_order_manager:
        from kalshi_edge.trader_engine import debug_order_manager

        debug_order_manager()
        return

    # --- Set up trader ---
    trader = None
    if args.trade:
        from kalshi_edge.trader_engine import SCHEMA as TRADER_SCHEMA, Trader

        http_trade = HttpClient(debug=args.debug_http)
        auth = None
        if not args.dry_run:
            from kalshi_edge.kalshi_auth import KalshiAuth

            if not args.api_key_id or not args.private_key_path:
                raise SystemExit("--trade requires --api-key-id and --private-key-path (or env vars), unless --dry-run")
            auth = KalshiAuth(api_key_id=args.api_key_id, private_key_path=args.private_key_path)

        base_fields = {
            "run_id": run_id,
            "config_hash": cfg_hash,
            "config_path": cfg_path,
            "git_commit": git_commit,
            "dry_run": bool(args.dry_run),
            "paper": bool(args.dry_run),
            "live": bool(args.trade and (not args.dry_run)),
            "subaccount": int(args.subaccount) if args.subaccount is not None else None,
        }

        trader = Trader(
            http=http_trade,
            auth=auth,
            kalshi_base_url=args.kalshi_base_url,
            state_file=state_file,
            trade_log_file=trade_log_path,
            dry_run=args.dry_run,
            config=cfg,
            subaccount=int(args.subaccount) if args.subaccount is not None else None,
            run_id=run_id,
            base_log_fields={**base_fields, "strategy_name": "trader", "strategy_schema_version": str(TRADER_SCHEMA)},
            strict_log_schema=bool(args.strict_log_schema),
            full_config_on_start={"config": config_to_dict(cfg)},
        )

    # --- Main loop ---
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
            if cfg.LOCK_EVENT and locked_event is not None:
                event = locked_event
            else:
                discovered = discover_current_event(window_minutes=cfg.WINDOW_MINUTES, debug_http=args.debug_http)
                event = discovered.event_ticker
                if cfg.LOCK_EVENT:
                    locked_event = event

        if args.watch and sys.stdout.isatty():
            print("[RUN] fetching prices + evaluating ladder (this can take a bit)...", flush=True)

        result = evaluate_event(
            event=event,
            url=url,
            max_strikes=cfg.MAX_STRIKES,
            band_pct=cfg.BAND_PCT,
            sort=cfg.SORT_MODE,
            fee_cents=cfg.FEE_CENTS,
            depth_window_cents=cfg.DEPTH_WINDOW_CENTS,
            threads=cfg.THREADS,
            iv_band_pct=cfg.IV_BAND_PCT,
            debug_http=args.debug_http,
        )
        last_result = result

        if cfg.LOCK_EVENT and locked_event is not None:
            if float(result.minutes_left) <= float(cfg.MIN_MINUTES_LEFT):
                locked_event = None

        if trader is not None and args.reconcile_state and not state_reconciled:
            trader.reconcile_state(result.event_ticker)
            state_reconciled = True

        render_once(
            result,
            sort_mode=cfg.SORT_MODE,
            compact=args.watch,
            show_explainer=not args.watch,
            fit_to_terminal=args.watch,
            clear_screen=args.watch,
        )

        if trader is not None:
            trader.on_tick(result)

    try:
        if args.watch:
            settlement_tracker = None
            if trader is not None and cfg.LOG_SETTLEMENT:
                from kalshi_edge.settlement_tracker import SettlementTracker

                settlement_tracker = SettlementTracker(http=HttpClient(debug=args.debug_http))
            while True:
                run_once()
                if settlement_tracker is not None and last_result is not None:
                    try:
                        settlement_tracker.maybe_log_settlements(trader=trader, log=getattr(trader, "log", None), active_event_ticker=str(last_result.event_ticker))
                    except Exception as e:
                        print(f"[settlement] check failed: {e}", flush=True)
                sleep(cfg.REFRESH_SECONDS)
        else:
            run_once()
    except KeyboardInterrupt:
        print("\n[RUN] Ctrl-C received, shutting down gracefully...")
        if trader is not None:
            try:
                trader.on_shutdown(last_result)
                print(f"[RUN] wrote shutdown snapshot to {trade_log_path}")
            except Exception as e:
                print(f"[RUN] failed to write shutdown snapshot: {e}")


if __name__ == "__main__":
    main()
