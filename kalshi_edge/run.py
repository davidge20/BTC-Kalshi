#!/usr/bin/env python3
"""
run.py
"""

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone
from time import sleep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", type=str, default=None)
    ap.add_argument("--url", type=str, default=None)
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--refresh-seconds", type=int, default=10)
    ap.add_argument("--window-minutes", type=int, default=70)
    ap.add_argument(
        "--max-strikes",
        type=int,
        default=None,
        help="Max strikes/orderbooks to fetch per tick. If unset, uses config MAX_STRIKES (or 10 if no config).",
    )
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
        default=None,
    )
    ap.add_argument(
        "--trade-log-file",
        type=str,
        default=None,
        help="Append-only JSONL log of trade actions + shutdown snapshot.",
    )
    ap.add_argument(
        "--trade-log-dir",
        type=str,
        default=None,
        help='Optional per-run log dir: <dir>/<YYYY-MM-DD>/<run_id>.jsonl (ignored if --trade-log-file is explicitly set).',
    )
    ap.add_argument("--strict-log-schema", action="store_true", help="Raise on missing required log fields (useful for tests).")
    ap.add_argument("--log-settlement", action="store_true", help="Periodically check settlement and emit event_settled once.")
    ap.add_argument("--subaccount", type=int, default=None)
    ap.add_argument("--reconcile-state", action="store_true")

    ap.add_argument("--api-key-id", type=str, default=os.environ.get("KALSHI_API_KEY_ID"))
    ap.add_argument("--private-key-path", type=str, default=os.environ.get("KALSHI_PRIVATE_KEY_PATH"))
    ap.add_argument("--kalshi-base-url", type=str, default=os.environ.get("KALSHI_BASE_URL"))

    args = ap.parse_args()

    # Strategy config (Workflow B). Load early so config errors fail fast.
    from kalshi_edge.strategy_config import ENV_VAR as CONFIG_ENV_VAR, config_hash, config_source_path, config_to_dict, load_config
    from kalshi_edge.trade_log import resolve_trade_log_path

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

    # Max strikes: allow CLI override; otherwise use config (or default).
    if args.max_strikes is None:
        args.max_strikes = int(getattr(cfg, "MAX_STRIKES", 10))

    run_id = str(uuid.uuid4())
    cfg_hash = config_hash(cfg)
    cfg_path = config_source_path()

    # State file: keep legacy default unless we are doing per-run logging.
    state_file = args.state_file or os.environ.get("KALSHI_EDGE_STATE_FILE", ".kalshi_edge_state.json")

    trade_log_file = args.trade_log_file or os.environ.get("KALSHI_EDGE_TRADE_LOG_FILE", "trade_log.jsonl")
    trade_log_dir = None if args.trade_log_file is not None else args.trade_log_dir
    if args.trade_log_dir and args.trade_log_file is not None:
        print("[log] ignoring --trade-log-dir because --trade-log-file was explicitly set", flush=True)
    trade_log_path = resolve_trade_log_path(
        trade_log_file=str(trade_log_file),
        trade_log_dir=str(trade_log_dir) if trade_log_dir else None,
        run_id=run_id,
        now_utc=datetime.now(timezone.utc),
    )
    print(f"[log] trade log -> {trade_log_path}", flush=True)

    # If the user enabled per-run logging and did not explicitly set a state file,
    # default the state file into logs/<date>/<run_id>.json (sibling to raw/).
    if args.state_file is None and trade_log_dir:
        dt = datetime.now(timezone.utc)
        day = dt.strftime("%Y-%m-%d")
        base_dir = str(trade_log_dir).rstrip("/").rstrip("\\")
        logs_root = os.path.dirname(base_dir) or base_dir
        state_file = os.path.join(logs_root, "state", day, f"{run_id}.json")
    print(f"[log] state file -> {state_file}", flush=True)

    def best_effort_git_commit() -> str | None:
        import subprocess

        def find_repo_root(start: str) -> str | None:
            cur = os.path.abspath(start)
            for _ in range(30):
                if os.path.exists(os.path.join(cur, ".git")):
                    return cur
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent
            return None

        repo_root = find_repo_root(os.getcwd()) or find_repo_root(os.path.dirname(__file__))
        if not repo_root:
            return None
        try:
            out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, stderr=subprocess.DEVNULL, text=True)
            s = out.strip()
            return s if s else None
        except Exception:
            return None

    git_commit = best_effort_git_commit()

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
        from kalshi_edge.trader_v2_engine import debug_order_manager

        debug_order_manager()
        return

    # Fees: if explicit maker/taker not provided, fall back to --fee-cents.
    if args.fee_cents_maker is None:
        args.fee_cents_maker = int(args.fee_cents)
    if args.fee_cents_taker is None:
        args.fee_cents_taker = int(args.fee_cents)

    trader = None
    if args.trade:
        from kalshi_edge.trader_v1 import V1Trader
        from kalshi_edge.trader_v2_engine import SCHEMA as V2_SCHEMA, V2Trader

        http_trade = HttpClient(debug=args.debug_http)
        auth = None
        if not args.dry_run:
            from kalshi_edge.kalshi_auth import KalshiAuth

            if not args.api_key_id or not args.private_key_path:
                raise SystemExit("--trade requires --api-key-id and --private-key-path (or env vars), unless --dry-run")
            auth = KalshiAuth(api_key_id=args.api_key_id, private_key_path=args.private_key_path)

        # Run envelope: merged into every log record by TradeLogger.
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

        if args.trader == "v1":
            trader = V1Trader(
                http=http_trade,
                auth=auth,
                kalshi_base_url=args.kalshi_base_url,
                state_file=state_file,
                trade_log_file=trade_log_path,
                fee_cents=int(args.fee_cents_taker),
                count=args.trade_count,
                dry_run=args.dry_run,
                run_id=run_id,
                base_log_fields={**base_fields, "strategy_name": "v1", "strategy_schema_version": "v1"},
                strict_log_schema=bool(args.strict_log_schema),
                subaccount=int(args.subaccount) if args.subaccount is not None else None,
                full_config_on_start={"config": config_to_dict(cfg)},
            )
        else:
            trader = V2Trader(
                http=http_trade,
                auth=auth,
                kalshi_base_url=args.kalshi_base_url,
                state_file=state_file,
                trade_log_file=trade_log_path,
                dry_run=args.dry_run,
                config=cfg,
                subaccount=int(args.subaccount) if args.subaccount is not None else None,
                run_id=run_id,
                base_log_fields={**base_fields, "strategy_name": "v2", "strategy_schema_version": str(V2_SCHEMA)},
                strict_log_schema=bool(args.strict_log_schema),
                full_config_on_start={"config": config_to_dict(cfg)},
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
            settlement_tracker = None
            if trader is not None and bool(args.log_settlement):
                from kalshi_edge.settlement_tracker import SettlementTracker

                settlement_tracker = SettlementTracker(http=HttpClient(debug=args.debug_http))
            while True:
                run_once()
                if settlement_tracker is not None and last_result is not None:
                    try:
                        settlement_tracker.maybe_log_settlements(trader=trader, log=getattr(trader, "log", None), active_event_ticker=str(last_result.event_ticker))
                    except Exception as e:
                        print(f"[settlement] check failed: {e}", flush=True)
                sleep(args.refresh_seconds)
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
