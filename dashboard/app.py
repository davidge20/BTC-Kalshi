from __future__ import annotations

import hashlib
import inspect
import json
import sys
import time
import os
import signal
import subprocess
import sqlite3
from threading import Event, Thread
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st


def _autorefresh(*, interval_ms: int, key: str) -> None:
    """
    Cross-version auto-refresh.

    Streamlit core does not expose `st.autorefresh` in many versions; the common
    implementation is the optional third-party `streamlit-autorefresh` package.
    """
    try:
        from streamlit_autorefresh import st_autorefresh  # type: ignore
    except Exception:
        # Degrade gracefully: no auto-refresh, user can manually rerun/refresh.
        return
    st_autorefresh(interval=int(interval_ms), key=str(key))

# Streamlit commonly places only this file's directory on sys.path.
# Ensure the repo root is importable so `import dashboard.*` works when running:
#   streamlit run dashboard/app.py
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dashboard.config import load_dashboard_config
from dashboard.storage import open_db
from dashboard.storage import queries as q
from dashboard.control.process_runner import ProcessHandle, read_log_tail, start_process, stop_process


def _pretty_json(d: Dict[str, Any]) -> str:
    return json.dumps(d, indent=2, sort_keys=True)


def _load_json_file(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    obj = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("Config must be a JSON object at top-level")
    return obj


def _write_text(path: str, s: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


def _latest_backtest_jsonl(log_dir: str) -> Optional[str]:
    p = Path(log_dir)
    if not p.is_absolute():
        p = Path(_REPO_ROOT) / p
    if not p.exists():
        return None
    files = sorted(p.glob("backtest_*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)
    return str(files[0]) if files else None


def _extract_backtest_log_path_from_process_log(text: str) -> Optional[str]:
    # backtest.py prints: "[backtest] log path: <path>"
    marker = "[backtest] log path:"
    for line in (text or "").splitlines()[::-1]:
        if marker in line:
            try:
                return line.split(marker, 1)[1].strip()
            except Exception:
                return None
    return None


def _read_latest_backtest_progress(backtest_jsonl_path: str) -> Optional[Dict[str, Any]]:
    p = Path(backtest_jsonl_path)
    if not p.is_absolute():
        p = Path(_REPO_ROOT) / p
    if not p.exists():
        return None
    try:
        data = p.read_bytes()
    except Exception:
        return None
    tail = data[-200_000:] if len(data) > 200_000 else data
    lines = tail.decode("utf-8", errors="ignore").splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("record_type") == "progress":
            return obj
    return None


def _pid_is_running(pid: Optional[int]) -> bool:
    if pid is None:
        return False
    try:
        pid_i = int(pid)
    except Exception:
        return False
    if pid_i <= 1:
        return False
    try:
        os.kill(pid_i, 0)
        return True
    except Exception:
        return False


def _pid_cmdline(pid: int) -> Optional[str]:
    """
    Best-effort command line lookup to avoid PID reuse bugs.
    """
    if os.name != "posix":
        return None
    try:
        out = subprocess.check_output(["ps", "-p", str(int(pid)), "-o", "command="], text=True)
    except Exception:
        return None
    s = (out or "").strip()
    return s or None


def _pid_is_backtest_process(pid: Optional[int]) -> bool:
    """
    Only treat a PID as "running" if it looks like our backtest subprocess.

    This prevents the dashboard from getting stuck if:
    - a prior run_dir/log was deleted, but we still have a stale pid in session_state
    - the OS later reuses that pid for an unrelated process
    """
    if not _pid_is_running(pid):
        return False
    try:
        pid_i = int(pid)  # type: ignore[arg-type]
    except Exception:
        return False
    cmd = _pid_cmdline(pid_i)
    if not cmd:
        return False
    return ("kalshi_edge.backtesting.backtest" in cmd) or ("-m kalshi_edge.backtesting.backtest" in cmd)


def _find_backtest_processes() -> list[tuple[int, str]]:
    """
    Return running backtest processes as [(pid, cmdline)].

    This is used as a fallback when Streamlit session_state is lost (refresh/restart),
    so the dashboard can still stop orphaned subprocesses it started earlier.
    """
    if os.name != "posix":
        return []
    try:
        out = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
    except Exception:
        return []
    rows: list[tuple[int, str]] = []
    for line in (out or "").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            pid_s, cmd = s.split(None, 1)
            pid_i = int(pid_s)
        except Exception:
            continue
        if pid_i <= 1:
            continue
        cmd = cmd.strip()
        if not cmd:
            continue
        if ("kalshi_edge.backtesting.backtest" in cmd) or ("-m kalshi_edge.backtesting.backtest" in cmd):
            rows.append((pid_i, cmd))
    return rows


def _recent_backtest_process_logs(max_n: int = 25) -> list[str]:
    """
    List recent `.dashboard/runs/*/process.log` paths, newest first.
    Useful when session_state was refreshed and we lost the active log path.
    """
    base = Path(_REPO_ROOT) / ".dashboard" / "runs"
    if not base.exists():
        return []
    logs = list(base.glob("*/process.log"))
    logs = [p for p in logs if p.exists()]
    logs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(p) for p in logs[: int(max_n)]]


def _mtime_caption(path: str) -> str:
    try:
        p = Path(path)
        ts = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        return f"last updated: `{ts}`"
    except Exception:
        return "last updated: unknown"


def _stop_pid(pid: Optional[int]) -> None:
    if pid is None:
        return
    try:
        pid_i = int(pid)
    except Exception:
        return
    if pid_i <= 1:
        return
    # Safety: avoid killing an unrelated process due to PID reuse.
    if not _pid_is_backtest_process(pid_i):
        return
    try:
        os.kill(pid_i, signal.SIGTERM)
    except Exception:
        return


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_json_loads(s: Any) -> Dict[str, Any]:
    if not isinstance(s, str) or not s:
        return {}
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _df(rows):
    return pd.DataFrame(rows) if rows else pd.DataFrame()

_SLOW_STEP_PRINT_S = float(os.getenv("DASHBOARD_SLOW_STEP_PRINT_S", "0.25"))


def _should_debug_timings() -> bool:
    v = str(os.getenv("DASHBOARD_DEBUG_TIMINGS", "")).strip().lower()
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    return bool(st.session_state.get("debug_timings", False))


def _timing_log(label: str, dt_s: float) -> None:
    # Always print slow steps; optionally print everything.
    debug = _should_debug_timings()
    if (not debug) and (float(dt_s) < float(_SLOW_STEP_PRINT_S)):
        return
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    run_n = int(st.session_state.get("_run_seq", 0))
    print(f"[dashboard][{now}][run={run_n}] {label}: {dt_s:.3f}s", flush=True)


def _timed(label: str, fn):
    t0 = time.perf_counter()
    out = fn()
    _timing_log(label, time.perf_counter() - t0)
    return out


def _st_call(fn, *args, **kwargs):
    """
    Call a Streamlit function while filtering kwargs for version compatibility.
    """
    try:
        sig = inspect.signature(fn)
        allowed = set(sig.parameters.keys())
        filtered = {k: v for k, v in kwargs.items() if k in allowed}
    except Exception:
        filtered = kwargs
    return fn(*args, **filtered)


def _st_dataframe(data, **kwargs):
    # Streamlit is migrating from `use_container_width` -> `width`.
    try:
        sig = inspect.signature(st.dataframe)
        params = set(sig.parameters.keys())
        if "width" in params:
            kwargs.setdefault("width", "stretch")
        elif "use_container_width" in params:
            kwargs.setdefault("use_container_width", True)
    except Exception:
        pass
    return _st_call(st.dataframe, data, **kwargs)


def _st_plotly_chart(fig, **kwargs):
    try:
        sig = inspect.signature(st.plotly_chart)
        params = set(sig.parameters.keys())
        if "width" in params:
            kwargs.setdefault("width", "stretch")
        elif "use_container_width" in params:
            kwargs.setdefault("use_container_width", True)
    except Exception:
        pass
    return _st_call(st.plotly_chart, fig, **kwargs)


st.set_page_config(page_title="kalshi_edge dashboard", layout="wide")
st.title("kalshi_edge — Local Dashboard")

st.session_state["_run_seq"] = int(st.session_state.get("_run_seq", 0)) + 1
_timing_log("script_start", 0.0)

cfg = _timed("load_dashboard_config", load_dashboard_config)

with st.sidebar:
    st.header("Data")
    db_path = st.text_input("SQLite DB path", value=str(cfg.db_path))
    st.caption("Tip: leave default to use `.dashboard/kalshi_edge_dashboard.sqlite`.")
    st.divider()
    st.subheader("Ingest JSONL")
    uploaded = st.file_uploader("Trade log (JSONL)", type=["jsonl", "txt", "log"])
    st.caption("You can ingest existing logs like `logs/dryrun_trades.jsonl` or `logs/v2_dryrun.jsonl`.")

if uploaded is not None:
    tmp_dir = Path(".dashboard")
    _timed("mkdir .dashboard", lambda: tmp_dir.mkdir(parents=True, exist_ok=True))
    uploaded_bytes = uploaded.getvalue()
    upload_digest = hashlib.sha256(uploaded_bytes).hexdigest()
    tmp_path = tmp_dir / f"upload_{upload_digest[:12]}.jsonl"

    # Auto-detect format: live trade JSONL uses keys (ts_utc,event), backtests use (record_type,ts,event,...)
    first_line = (uploaded_bytes.decode("utf-8", errors="ignore").splitlines()[:1] or [""])[0].strip()
    is_backtest = False
    try:
        obj = json.loads(first_line) if first_line else {}
        is_backtest = isinstance(obj, dict) and ("record_type" in obj)
    except Exception:
        is_backtest = False

    # Avoid accidental re-ingest loops on reruns (e.g., after file-change rerun).
    already_ingested = st.session_state.get("uploaded_ingested_digest") == upload_digest
    reingest = st.button("Re-ingest uploaded file", disabled=not already_ingested)
    should_ingest = (not already_ingested) or bool(reingest)
    if already_ingested and not reingest:
        st.info("This upload was already ingested. Choose a different file or click **Re-ingest uploaded file**.")

    if should_ingest:
        _timed("write uploaded tmp file", lambda: tmp_path.write_bytes(uploaded_bytes))
        with st.spinner("Ingesting upload into SQLite…"):
            if is_backtest:
                from dashboard.ingest.ingest_backtest_jsonl import ingest_backtest_jsonl

                run_id = f"backtest-upload-{upload_digest[:12]}"
                n = _timed(
                    "ingest_backtest_jsonl(upload)",
                    lambda: ingest_backtest_jsonl(input_path=str(tmp_path), db_path=db_path, run_id=run_id),
                )
                st.success(f"Ingested {n} backtest records into `{db_path}` from upload (run_id=`{run_id}`).")
            else:
                from dashboard.ingest.ingest_jsonl import ingest_jsonl

                n = _timed("ingest_jsonl(upload)", lambda: ingest_jsonl(input_path=str(tmp_path), db_path=db_path))
                st.success(f"Ingested {n} events into `{db_path}` from upload.")

        st.session_state["uploaded_ingested_digest"] = upload_digest


db_file = Path(db_path)
if not db_file.exists():
    st.warning("No dashboard DB found. Ingest a JSONL file to populate it.")
    st.stop()

try:
    conn = _timed("open_db (connect+migrate)", lambda: open_db(db_path))
except sqlite3.DatabaseError as e:
    st.error(f"The path `{db_path}` exists but is not a valid SQLite database (or is corrupted).")
    st.caption("Tip: make sure the sidebar **SQLite DB path** points to `.dashboard/kalshi_edge_dashboard.sqlite`.")
    st.code(str(e))
    st.stop()

run_ids = _timed("q.list_run_ids", lambda: q.list_run_ids(conn))
if not run_ids:
    st.warning("DB exists but no `run_id` found yet. Ingest a trade log or backtest JSONL.")
    st.caption("Tip: newer logs include `run_id`; backtests ingested via the dashboard always set one.")

with st.sidebar:
    st.header("Filters")
    st.checkbox("Debug: print load timings to terminal", value=bool(st.session_state.get("debug_timings", False)), key="debug_timings")
    run_opts = (["(all runs)"] + run_ids) if run_ids else ["(all runs / unlabeled)"]
    selected_run_opt = st.selectbox("Run (run_id)", run_opts, index=1 if len(run_opts) > 1 else 0)
    selected_run = None if selected_run_opt.startswith("(all runs") else selected_run_opt
    is_backtest_run = bool(selected_run and _timed("q.run_is_backtest", lambda: q.run_is_backtest(conn, run_id=selected_run)))

    if selected_run:
        event_tickers = _timed("q.list_event_tickers_for_run", lambda: q.list_event_tickers_for_run(conn, run_id=selected_run))
    else:
        event_tickers = _timed("q.list_event_tickers(all)", lambda: q.list_event_tickers(conn))
    if not event_tickers:
        # Fallback for older logs that didn't record run_id consistently.
        event_tickers = _timed("q.list_event_tickers(fallback)", lambda: q.list_event_tickers(conn))

    if not event_tickers:
        st.warning("No `event_ticker` found yet. Ingest a trade log or backtest JSONL.")
        st.stop()

    st.caption("Event picker below affects **Market Edge** only.")
    selected_event = st.selectbox("Event (used by Market Edge tab)", event_tickers, index=0)
    edge_threshold = st.slider("Edge threshold (edge_pp)", min_value=-0.20, max_value=0.30, value=0.05, step=0.01)
    refresh = st.checkbox("Auto-refresh", value=False)
    refresh_seconds = st.number_input("Refresh interval (seconds)", min_value=0.0, value=float(cfg.refresh_seconds), step=1.0)

if selected_run is None:
    st.warning("Viewing **ALL runs**. This can mix backtest and paper/live data.")
elif is_backtest_run:
    st.info(f"Viewing **BACKTEST** run `{selected_run}`. These tabs show *backtested / historical* results (not live trading).")
else:
    st.warning(f"Viewing run `{selected_run}` from ingested logs. This may include paper or live trading depending on how the log was produced.")

if refresh and refresh_seconds > 0:
    st.caption(f"Last refresh: {_now_utc_iso()} — refreshing every {refresh_seconds:.1f}s")
    _autorefresh(interval_ms=int(refresh_seconds * 1000), key="autorefresh")


tab_market, tab_orders, tab_risk, tab_perf, tab_sys, tab_control = st.tabs(
    ["Market Edge", "Orders (run)", "Portfolio/Risk (run)", "Performance (run)", "System (run)", "Control Center"]
)


with tab_market:
    st.subheader("Market + Model Edge")

    latest_ts = _timed(
        "q.latest_candidate_timestamp",
        lambda: q.latest_candidate_timestamp(conn, event_ticker=selected_event, run_id=selected_run),
    )
    if latest_ts is None:
        st.info("No `candidate`/`skip` records ingested yet for this event.")
    else:
        ts_choices = _timed(
            "q.candidate_timestamps",
            lambda: q.candidate_timestamps(conn, event_ticker=selected_event, run_id=selected_run, limit=300),
        )
        use_exact = st.checkbox("Select exact snapshot timestamp (recommended for backtests)", value=False)
        selected_ts = None
        if use_exact and ts_choices:
            selected_ts = st.selectbox("Snapshot ts_utc", ts_choices, index=0)

        if selected_ts:
            st.caption(f"Using snapshot at `ts_utc={selected_ts}`")
            df = _df(
                _timed(
                    "q.candidates_at_ts",
                    lambda: q.candidates_at_ts(conn, event_ticker=selected_event, ts_utc=selected_ts, run_id=selected_run),
                )
            )
        else:
            st.caption(f"Using best-effort latest ladder snapshot near `ts_utc={latest_ts}`")
            recent = _df(
                _timed(
                    "q.candidates_latest_n",
                    lambda: q.candidates_latest_n(conn, event_ticker=selected_event, run_id=selected_run, limit=4000),
                )
            )
            if recent.empty:
                df = recent
            else:
                # For each (strike, side), keep the most recent record.
                recent["ts"] = pd.to_datetime(recent["ts_utc"], utc=True, errors="coerce")
                recent = recent.sort_values("ts", ascending=False)
                df = recent.drop_duplicates(subset=["strike", "side"], keep="first").sort_values(["strike", "side"])

        if df.empty:
            st.info("No ladder rows found at that timestamp.")
        else:
            # Build per-strike view from YES-side rows (implied_q_yes is shared)
            yes = df[df["side"] == "yes"].copy()
            yes = yes.sort_values("strike")
            if yes.empty:
                st.info("No YES-side ladder rows available to chart.")
            else:
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=yes["strike"], y=yes["p_model"], mode="lines+markers", name="p_model (YES prob)"))
                fig.add_trace(go.Scatter(x=yes["strike"], y=yes["implied_q_yes"], mode="lines+markers", name="Kalshi implied q_yes"))

                # Uncertainty band (Phase 1 placeholder): +/- 0.05 clipped
                band = 0.05
                upper = (yes["p_model"] + band).clip(0, 1)
                lower = (yes["p_model"] - band).clip(0, 1)
                fig.add_trace(go.Scatter(x=yes["strike"], y=upper, mode="lines", name="p_model + band", line=dict(width=1, dash="dot")))
                fig.add_trace(go.Scatter(x=yes["strike"], y=lower, mode="lines", name="p_model - band", line=dict(width=1, dash="dot")))

                fig.update_layout(
                    height=420,
                    xaxis_title="Strike",
                    yaxis_title="Probability",
                    yaxis=dict(range=[0, 1]),
                    legend=dict(orientation="h"),
                )
                _st_plotly_chart(fig)

            st.divider()
            st.subheader("EV / Break-even")
            # Show top edges and implied break-even
            df2 = df.copy()
            df2["breakeven_p_win"] = (df2["price_cents"].fillna(0) + df2["fee_cents"].fillna(0)) / 100.0
            df2["edge_ok"] = df2["edge_pp"].fillna(-999) >= float(edge_threshold)
            _st_dataframe(
                df2.sort_values(["edge_ok", "edge_pp"], ascending=[False, False])[
                    [
                        "market_ticker",
                        "strike",
                        "side",
                        "price_cents",
                        "fee_cents",
                        "p_model",
                        "implied_q_yes",
                        "edge_pp",
                        "ev",
                        "breakeven_p_win",
                        "spread_cents",
                        "top_size",
                        "minutes_left",
                        "spot",
                        "sigma_blend",
                        "kind",
                    ]
                ].head(200),
                hide_index=True,
            )

            st.divider()
            st.subheader("Strike detail (model vs implied over time)")

            markets = _timed(
                "q.candidate_markets_for_event",
                lambda: q.candidate_markets_for_event(conn, event_ticker=selected_event, run_id=selected_run, side="yes", limit=800),
            )
            if not markets:
                st.info("No candidate ladder markets found to drill into.")
            else:
                def _fmt_market_row(r: Dict[str, Any]) -> str:
                    mt = str(r.get("market_ticker") or "")
                    strike = r.get("strike")
                    try:
                        s = float(strike) if strike is not None else None
                    except Exception:
                        s = None
                    if s is None:
                        return mt
                    # Favor a compact strike label but keep ticker for uniqueness.
                    return f"T={s:g} — {mt}"

                labels = [_fmt_market_row(r) for r in markets]
                label_to_mt = {lbl: str(r.get("market_ticker")) for lbl, r in zip(labels, markets)}

                chosen_label = st.selectbox("Market / strike", labels, index=0)
                chosen_mt = label_to_mt.get(chosen_label)

                if not chosen_mt:
                    st.info("Select a market to view its series.")
                else:
                    run_ids = _timed(
                        "q.candidate_run_ids_for_market",
                        lambda: q.candidate_run_ids_for_market(conn, market_ticker=chosen_mt, limit=100),
                    )
                    run_choice = None
                    if len(run_ids) >= 2:
                        # Default to the currently selected run if present.
                        opts = ["(all)"] + run_ids
                        default_idx = 0
                        try:
                            if selected_run in run_ids:
                                default_idx = opts.index(selected_run)
                        except Exception:
                            default_idx = 0
                        run_choice = st.selectbox("Run (optional; helps avoid mixing multiple ingests)", opts, index=default_idx)
                        if run_choice == "(all)":
                            run_choice = None

                    series = _df(
                        _timed(
                            "q.candidate_series_for_market",
                            lambda: q.candidate_series_for_market(conn, market_ticker=chosen_mt, side="yes", run_id=run_choice, limit=20_000),
                        )
                    )
                    if series.empty:
                        st.info("No time series rows found for that market.")
                    else:
                        try:
                            n_snap = int(series["ts_utc"].nunique())
                        except Exception:
                            n_snap = 0
                        st.caption(f"Data points: **{len(series)} rows** across **{n_snap} snapshot(s)**.")
                        if n_snap <= 1:
                            st.info(
                                "This market only has a single recorded snapshot, so the time-series can’t be a smooth curve. "
                                "For backtests, you typically want `backtest.LOG_LADDER=true` and `backtest.LOG_LADDER_EVERY_N=1`, "
                                "and enough `backtest.MAX_STRIKES`/`backtest.BAND_PCT` so this strike is included on each tick."
                            )
                        series["ts"] = pd.to_datetime(series["ts_utc"], utc=True, errors="coerce")
                        series = series.dropna(subset=["ts"]).sort_values("ts")

                        fig = go.Figure()
                        fig.add_trace(
                            go.Scatter(
                                x=series["ts"],
                                y=series["p_model"],
                                mode="lines+markers",
                                name="p_model (YES prob)",
                            )
                        )
                        fig.add_trace(
                            go.Scatter(
                                x=series["ts"],
                                y=series["implied_q_yes"],
                                mode="lines+markers",
                                name="Kalshi implied q_yes",
                            )
                        )
                        fig.update_layout(
                            height=420,
                            xaxis_title="Time (UTC)",
                            yaxis_title="Probability",
                            yaxis=dict(range=[0, 1]),
                            legend=dict(orientation="h"),
                        )
                        _st_plotly_chart(fig)

                        st.caption("Latest rows (for exact numbers).")
                        cols = [
                            "ts_utc",
                            "run_id",
                            "event_ticker",
                            "market_ticker",
                            "strike",
                            "price_cents",
                            "fee_cents",
                            "p_model",
                            "implied_q_yes",
                            "edge_pp",
                            "ev",
                            "spread_cents",
                            "minutes_left",
                            "spot",
                            "sigma_blend",
                            "source",
                            "kind",
                        ]
                        keep = [c for c in cols if c in series.columns]
                        _st_dataframe(series.sort_values("ts", ascending=False)[keep].head(250), hide_index=True)


with tab_orders:
    st.subheader("Orders + Execution")
    st.caption(("Scope: **all runs**" if selected_run is None else f"Scope: run `{selected_run}`") + " (all events)")

    open_rows = _timed(
        "q.open_orders_for_run" if selected_run else "q.open_orders",
        lambda: (q.open_orders_for_run(conn, run_id=selected_run) if selected_run else q.open_orders(conn)),
    )
    df_open = _df(open_rows)
    if df_open.empty:
        st.info("No open/resting orders inferred from recent order lifecycle events.")
    else:
        df_open["age_minutes"] = (
            (pd.Timestamp.now(tz="UTC") - pd.to_datetime(df_open["ts_utc"], utc=True)).dt.total_seconds() / 60.0
        )
        _st_dataframe(
            df_open[
                ["ts_utc", "order_id", "client_order_id", "event_ticker", "market_ticker", "side", "action", "status", "price_cents", "count", "age_minutes"]
            ].head(200),
            hide_index=True,
        )

        order_ids = [x for x in df_open["order_id"].dropna().astype(str).unique().tolist() if x]
        if order_ids:
            oid = st.selectbox("Order timeline (order_id)", order_ids)
            tl = _df(q.order_timeline(conn, oid))
            _st_dataframe(
                tl[["ts_utc", "order_id", "event_ticker", "market_ticker", "side", "action", "status", "price_cents", "count", "remaining_count", "delta_fill_count"]],
                hide_index=True,
            )

    st.divider()
    st.subheader("Fills")
    if selected_run:
        fills = _df(
            _timed(
                "q.fills_for_run",
                lambda: q.fills_for_run(conn, run_id=selected_run, event_ticker=None, limit=50_000),
            )
        )
    else:
        fills = _df(_timed("q.fills_recent(all_runs)", lambda: q.fills_recent(conn, minutes=200_000, event_ticker=None)))
    if fills.empty:
        st.info("No fills found for this scope.")
    else:
        _st_dataframe(
            fills[
                [
                    "ts_utc",
                    "event_ticker",
                    "market_ticker",
                    "side",
                    "fill_kind",
                    "count",
                    "price_cents",
                    "fee_cents",
                    "edge_pp",
                    "pnl_total",
                    "pnl_per_contract",
                ]
            ].head(300),
            hide_index=True,
        )

        # Simple slippage diagnostic: exit - entry for each market_ticker (best-effort)
        exits = fills[fills["fill_kind"] == "exit"].copy()
        entries = fills[fills["fill_kind"].isin(["entry", "scale_in"])].copy()
        if not exits.empty and not entries.empty:
            ent = entries.groupby(["market_ticker", "side"], as_index=False)["price_cents"].mean().rename(columns={"price_cents": "entry_price_cents"})
            ex = exits.groupby(["market_ticker", "side"], as_index=False)["price_cents"].mean().rename(columns={"price_cents": "exit_price_cents"})
            merged = ent.merge(ex, on=["market_ticker", "side"], how="inner")
            merged["delta_cents"] = merged["exit_price_cents"] - merged["entry_price_cents"]
            fig = px.histogram(merged, x="delta_cents", nbins=30, title="Exit - Entry (cents), best-effort")
            _st_plotly_chart(fig)


with tab_risk:
    st.subheader("Portfolio + Risk (best-effort from fills)")
    st.caption(("Scope: **all runs**" if selected_run is None else f"Scope: run `{selected_run}`") + " (all events)")

    if selected_run:
        fills = _df(
            _timed(
                "q.fills_for_run(risk)",
                lambda: q.fills_for_run(conn, run_id=selected_run, event_ticker=None, limit=200_000),
            )
        )
    else:
        fills = _df(_timed("q.fills_recent(risk all_runs)", lambda: q.fills_recent(conn, minutes=500_000, event_ticker=None)))
    if fills.empty:
        st.info("No fills available to infer positions.")
    else:
        f = fills.copy()
        f["signed_count"] = f["count"].astype(float)
        f.loc[f["fill_kind"] == "exit", "signed_count"] *= -1.0
        pos = f.groupby(["market_ticker", "side"], as_index=False)["signed_count"].sum().rename(columns={"signed_count": "net_contracts"})
        pos = pos.sort_values("net_contracts", ascending=False)
        _st_dataframe(pos, hide_index=True)

        fig = px.bar(pos, x="market_ticker", y="net_contracts", color="side", title="Net contracts by market/side")
        fig.update_layout(height=360, xaxis_title="", yaxis_title="Net contracts")
        _st_plotly_chart(fig)

    st.divider()
    st.subheader("Risk lights")
    health = _df(_timed("q.system_health_latest(risk)", lambda: q.system_health_latest(conn, run_id=selected_run, limit=200)))
    last_spot = health[health["metric"] == "tick.spot"].head(1)
    if not last_spot.empty:
        ts = pd.to_datetime(last_spot.iloc[0]["ts_utc"], utc=True)
        age_s = (pd.Timestamp.now(tz="UTC") - ts).total_seconds()
        stale = age_s > 60
        st.write(f"Spot freshness: **{age_s:.0f}s ago** ({'STALE' if stale else 'OK'})")
    else:
        st.write("Spot freshness: **unknown** (no `tick_summary` ingested).")


with tab_perf:
    st.subheader("Performance")
    st.caption(("Scope: **all runs**" if selected_run is None else f"Scope: run `{selected_run}`") + " (all events)")

    if selected_run:
        pnl = _df(
            _timed(
                "q.pnl_series_for_run",
                lambda: q.pnl_series_for_run(conn, run_id=selected_run, event_ticker=None, limit=50_000),
            )
        )
        fills_all = _df(
            _timed(
                "q.fills_for_run(perf)",
                lambda: q.fills_for_run(conn, run_id=selected_run, event_ticker=None, limit=200_000),
            )
        )
    else:
        pnl = _df(_timed("q.pnl_series(all_runs)", lambda: q.pnl_series(conn, event_ticker=None, limit=50_000)))
        fills_all = _df(_timed("q.fills_recent(perf all_runs)", lambda: q.fills_recent(conn, minutes=500_000, event_ticker=None)))

    # Prefer pnl table when present (backtests emit event_summary/run_summary).
    series = None
    if not pnl.empty and "total" in pnl.columns:
        p = pnl.copy()
        p["ts"] = pd.to_datetime(p["ts_utc"], utc=True, errors="coerce")
        p["total"] = pd.to_numeric(p["total"], errors="coerce")
        p = p.dropna(subset=["ts", "total"]).sort_values("ts")
        # Aggregate across events/markets at each timestamp for run-level view.
        series = p.groupby("ts", as_index=False)["total"].sum().rename(columns={"total": "pnl_step"})
        series["cum_pnl"] = series["pnl_step"].cumsum()
    elif not fills_all.empty and "pnl_total" in fills_all.columns:
        ex = fills_all[fills_all["fill_kind"] == "exit"].copy()
        ex["ts"] = pd.to_datetime(ex["ts_utc"], utc=True, errors="coerce")
        ex["pnl_total"] = pd.to_numeric(ex["pnl_total"], errors="coerce")
        ex = ex.dropna(subset=["ts", "pnl_total"]).sort_values("ts")
        if not ex.empty:
            series = ex.groupby("ts", as_index=False)["pnl_total"].sum().rename(columns={"pnl_total": "pnl_step"})
            series["cum_pnl"] = series["pnl_step"].cumsum()

    if series is None or series.empty:
        st.info("No PnL series available for this scope yet.")
    else:
        fig = px.line(series, x="ts", y="cum_pnl", title="Cumulative PnL (run-level, best-effort)")
        _st_plotly_chart(fig)

    st.divider()
    st.subheader("Calibration / reliability (best-effort)")
    fills = fills_all
    if fills.empty:
        st.info("Need fills to compute calibration.")
    else:
        # Use entry fills where payload had p_yes/p and infer win from pnl on exit fill in same market.
        fills["payload"] = fills["payload_json"].apply(_safe_json_loads)
        entry = fills[fills["fill_kind"].isin(["entry", "scale_in"])].copy()
        exitf = fills[fills["fill_kind"] == "exit"].copy()
        if entry.empty or exitf.empty:
            st.info("Need both entry and exit fills to compute a simple reliability proxy.")
        else:
            entry["p_yes"] = entry["payload"].apply(lambda d: d.get("p_yes", d.get("p")))
            entry["p_yes"] = pd.to_numeric(entry["p_yes"], errors="coerce")
            # Side-specific win probability
            entry["p_win"] = entry["p_yes"]
            entry.loc[entry["side"] == "no", "p_win"] = 1.0 - entry.loc[entry["side"] == "no", "p_yes"]

            ex = exitf.groupby(["market_ticker", "side"], as_index=False)["pnl_total"].sum()
            ex["win"] = (ex["pnl_total"] > 0).astype(int)
            merged = entry.groupby(["market_ticker", "side"], as_index=False)["p_win"].mean().merge(ex, on=["market_ticker", "side"], how="inner")
            merged = merged.dropna(subset=["p_win"])
            if merged.empty:
                st.info("Not enough data with p_win to compute reliability.")
            else:
                merged["bucket"] = pd.cut(merged["p_win"], bins=[0, 0.2, 0.4, 0.6, 0.8, 1.0], include_lowest=True)
                rel = merged.groupby("bucket", as_index=False).agg(n=("win", "count"), win_rate=("win", "mean"), avg_p=("p_win", "mean"))
                fig = px.scatter(rel, x="avg_p", y="win_rate", size="n", title="Reliability (proxy): avg p_win vs win rate")
                fig.update_layout(yaxis=dict(range=[0, 1]), xaxis=dict(range=[0, 1]))
                _st_plotly_chart(fig)
                _st_dataframe(rel, hide_index=True)

    st.divider()
    st.subheader("Edge-to-outcome")
    if fills.empty:
        st.info("Need fills to compute edge-to-outcome.")
    else:
        exitf = fills[fills["fill_kind"] == "exit"].copy()
        if exitf.empty:
            st.info("No exit fills yet.")
        else:
            fig = px.scatter(
                exitf,
                x="edge_pp",
                y="pnl_per_contract",
                hover_data=["market_ticker", "side", "ts_utc"],
                title="Edge at entry (if logged) vs realized pnl/contract (from exit)",
            )
            _st_plotly_chart(fig)

    st.divider()
    st.subheader("Trade journal")
    if fills.empty:
        st.info("No trades yet.")
    else:
        _st_dataframe(
            fills[
                ["ts_utc", "market_ticker", "side", "fill_kind", "count", "price_cents", "edge_pp", "pnl_total", "pnl_per_contract"]
            ].head(500),
            hide_index=True,
        )


with tab_sys:
    st.subheader("System health")
    health = _df(_timed("q.system_health_latest(sys)", lambda: q.system_health_latest(conn, run_id=selected_run, limit=300)))
    if health.empty:
        st.info("No health metrics ingested yet.")
    else:
        _st_dataframe(health[["ts_utc", "metric", "value_num", "value_text"]].head(200), hide_index=True)

    st.divider()
    st.subheader("Latest errors (best-effort)")
    if not health.empty:
        errs = health[health["metric"] == "error"].copy()
        if errs.empty:
            st.write("No errors ingested.")
        else:
            _st_dataframe(errs[["ts_utc", "value_text"]].head(50), hide_index=True)

conn.close()


with tab_control:
    st.subheader("Control Center (Backtest / Paper / Live)")
    st.caption("This runs the existing CLI under the hood. Backtest is safe; live trading can place real orders.")

    if "config_path" not in st.session_state:
        st.session_state["config_path"] = "strategy_config.example.json"
    if "config_dict" not in st.session_state:
        try:
            st.session_state["config_dict"] = _load_json_file(st.session_state["config_path"])
        except Exception:
            st.session_state["config_dict"] = {"strategy": {}, "backtest": {}}
    if "active_config_path" not in st.session_state:
        st.session_state["active_config_path"] = ".dashboard/active_config.json"

    # --- mode banner
    current_mode = st.session_state.get("run_mode") or "idle"
    if current_mode == "live":
        st.error("MODE: LIVE TRADING (real orders possible)")
    elif current_mode == "paper":
        st.warning("MODE: PAPER TRADING (dry-run)")
    elif current_mode == "backtest":
        st.info("MODE: BACKTEST (historical)")
    else:
        st.success("MODE: IDLE")

    st.divider()

    col_a, col_b = st.columns([1, 1])
    with col_a:
        st.markdown("**Config file**")
        st.session_state["config_path"] = st.text_input("Load config path", value=str(st.session_state["config_path"]))
        if st.button("Load config from file"):
            try:
                st.session_state["config_dict"] = _load_json_file(str(st.session_state["config_path"]))
                st.success("Loaded config.")
            except Exception as e:
                st.error(f"Failed to load config: {e}")

        cfg_text = st.text_area("Edit config JSON (strategy + backtest)", value=_pretty_json(st.session_state["config_dict"]), height=320)
        if st.button("Apply JSON edits"):
            try:
                obj = json.loads(cfg_text)
                if not isinstance(obj, dict):
                    raise ValueError("Top-level JSON must be an object")
                st.session_state["config_dict"] = obj
                st.success("Applied JSON edits.")
            except Exception as e:
                st.error(f"Invalid JSON: {e}")

        st.session_state["active_config_path"] = st.text_input("Active config output path", value=str(st.session_state["active_config_path"]))
        if st.button("Save active config"):
            try:
                _write_text(str(st.session_state["active_config_path"]), _pretty_json(st.session_state["config_dict"]))
                st.success(f"Saved `{st.session_state['active_config_path']}`")
            except Exception as e:
                st.error(f"Failed to save: {e}")

    with col_b:
        st.markdown("**Quick knobs (common)**")
        d = st.session_state["config_dict"]
        d.setdefault("strategy", {})
        d.setdefault("backtest", {})

        s = d["strategy"]
        bt = d["backtest"]

        s["MIN_EV"] = st.number_input("strategy.MIN_EV", value=float(s.get("MIN_EV", 0.04)), step=0.01)
        s["ORDER_SIZE"] = st.number_input("strategy.ORDER_SIZE", value=int(s.get("ORDER_SIZE", 1)), step=1)
        s["FEE_CENTS"] = st.number_input("strategy.FEE_CENTS", value=int(s.get("FEE_CENTS", 1)), step=1)
        s["MAX_STRIKES"] = st.number_input("strategy.MAX_STRIKES", value=int(s.get("MAX_STRIKES", 10)), step=1)
        s["REFRESH_SECONDS"] = st.number_input("strategy.REFRESH_SECONDS", value=int(s.get("REFRESH_SECONDS", 10)), step=1)

        bt["DAYS"] = st.number_input("backtest.DAYS", value=int(bt.get("DAYS", 14)), step=1)
        _start_default = "" if bt.get("START_DATE") in (None, "") else str(bt.get("START_DATE"))
        _end_default = "" if bt.get("END_DATE") in (None, "") else str(bt.get("END_DATE"))
        bt["START_DATE"] = st.text_input("backtest.START_DATE (YYYY-MM-DD, blank for null)", value=_start_default)
        bt["END_DATE"] = st.text_input("backtest.END_DATE (YYYY-MM-DD, blank for null)", value=_end_default)
        bt["MAX_EVENTS"] = st.number_input("backtest.MAX_EVENTS", value=int(bt.get("MAX_EVENTS", 50)), step=10)
        bt["LOG_DIR"] = st.text_input("backtest.LOG_DIR", value=str(bt.get("LOG_DIR", "backtests")))

        # normalize null strings for dates
        for k in ("START_DATE", "END_DATE"):
            v = bt.get(k)
            if isinstance(v, str) and v.strip().lower() in {"null", "none", ""}:
                bt[k] = None

        st.session_state["config_dict"] = d
        st.markdown("**Parsed summary**")
        st.json(
            {
                "strategy": {k: s.get(k) for k in ["MIN_EV", "ORDER_SIZE", "FEE_CENTS", "MAX_STRIKES", "REFRESH_SECONDS"]},
                "backtest": {k: bt.get(k) for k in ["DAYS", "START_DATE", "END_DATE", "MAX_EVENTS", "LOG_DIR"]},
            }
        )

    st.divider()

    sub_backtest, sub_trading = st.tabs(["Backtest", "Paper / Live trading"])

    # -----------------
    # Backtest runner
    # -----------------
    with sub_backtest:
        st.markdown("**Run historical backtest**")
        ingest_after = st.checkbox("Auto-ingest backtest results into dashboard DB", value=True)

        # Streamlit only updates output on rerun. Keep a light auto-refresh toggle available
        # so logs/progress update while the subprocess runs.
        if "bt_autorefresh_enabled" not in st.session_state:
            st.session_state["bt_autorefresh_enabled"] = True
        if "bt_autorefresh_seconds" not in st.session_state:
            st.session_state["bt_autorefresh_seconds"] = 1.0

        col_r1, col_r2, col_r3 = st.columns([1, 1, 2])
        with col_r1:
            st.session_state["bt_autorefresh_enabled"] = st.checkbox(
                "Auto-refresh backtest tab", value=bool(st.session_state["bt_autorefresh_enabled"])
            )
        with col_r2:
            st.session_state["bt_autorefresh_seconds"] = st.number_input(
                "Refresh (seconds)",
                min_value=0.5,
                value=float(st.session_state["bt_autorefresh_seconds"]),
                step=0.5,
            )
        with col_r3:
            st.caption("Tip: leave this on to see `process.log` update while the backtest runs.")

        backtest_handle: Optional[ProcessHandle] = st.session_state.get("backtest_handle")
        backtest_pid = st.session_state.get("backtest_pid")
        backtest_log_path = st.session_state.get("backtest_log_path")
        discovered = _find_backtest_processes()
        backtest_running = (backtest_handle is not None and backtest_handle.popen.poll() is None) or _pid_is_backtest_process(backtest_pid) or bool(discovered)

        if bool(st.session_state.get("bt_autorefresh_enabled")) and float(st.session_state.get("bt_autorefresh_seconds") or 0) > 0:
            # Only auto-refresh while a backtest is actively running.
            # (Otherwise this can cause confusing "background reruns" even with sidebar Auto-refresh off.)
            if backtest_running:
                _autorefresh(interval_ms=int(float(st.session_state["bt_autorefresh_seconds"]) * 1000), key="backtest_autorefresh")

        if backtest_running:
            st.info("Backtest is running…")
            if discovered and (backtest_pid is None or not _pid_is_backtest_process(backtest_pid)):
                with st.expander("Detected running backtests (from `ps`)", expanded=True):
                    st.caption("If you refreshed the page, these may be orphaned runs from earlier sessions.")
                    for pid, cmd in discovered:
                        c1, c2, c3 = st.columns([1, 7, 1])
                        with c1:
                            st.code(str(pid))
                        with c2:
                            st.code(cmd)
                        with c3:
                            if st.button("Stop", key=f"stop_backtest_pid_{pid}"):
                                _stop_pid(pid)
                                for k in ("backtest_handle", "backtest_pid", "backtest_log_path"):
                                    try:
                                        st.session_state.pop(k, None)
                                    except Exception:
                                        pass
                                st.success(f"Sent SIGTERM to backtest pid={pid}.")
            if st.button("Stop backtest"):
                if backtest_handle is not None:
                    stop_process(backtest_handle)
                else:
                    _stop_pid(backtest_pid)
                # Best-effort cleanup: if session state was stale, clear it.
                for k in ("backtest_handle", "backtest_pid", "backtest_log_path"):
                    try:
                        st.session_state.pop(k, None)
                    except Exception:
                        pass
            if discovered and st.button("Stop ALL running backtests"):
                for pid, _cmd in discovered:
                    _stop_pid(pid)
                for k in ("backtest_handle", "backtest_pid", "backtest_log_path"):
                    try:
                        st.session_state.pop(k, None)
                    except Exception:
                        pass
        else:
            if st.button("Run backtest now", type="primary"):
                # write config snapshot for this run
                run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                run_dir = Path(_REPO_ROOT) / ".dashboard" / "runs" / f"backtest_{run_ts}"
                run_dir.mkdir(parents=True, exist_ok=True)
                cfg_path = str(run_dir / "config.json")
                _write_text(cfg_path, _pretty_json(st.session_state["config_dict"]))

                log_path = str(run_dir / "process.log")
                args = [sys.executable, "-u", "-m", "kalshi_edge.backtesting.backtest", "--config", cfg_path]
                h = start_process(name="backtest", args=args, cwd=str(_REPO_ROOT), log_path=log_path)
                st.session_state["backtest_handle"] = h
                st.session_state["backtest_pid"] = int(h.popen.pid)
                st.session_state["backtest_log_path"] = str(h.log_path)
                st.session_state["backtest_run_dir"] = str(run_dir)
                st.session_state["run_mode"] = "backtest"
                st.session_state["backtest_ingested"] = False
                st.success("Backtest started.")
                # Force an immediate rerun so UI switches to "running" and log/progress appear.
                st.rerun()

        # Always show the log box once we've started at least one backtest.
        backtest_pid = st.session_state.get("backtest_pid")
        backtest_log_path = st.session_state.get("backtest_log_path")
        # If session state was lost, allow selecting a recent process.log to view.
        chosen_log_path = backtest_log_path
        recent_logs = _recent_backtest_process_logs()
        if not chosen_log_path and recent_logs:
            chosen_log_path = st.selectbox("Select a backtest process log to view", recent_logs, index=0)

        if chosen_log_path:
            st.caption(f"Log file: `{chosen_log_path}` (pid={backtest_pid}) — {_mtime_caption(chosen_log_path)}")
            tail = read_log_tail(str(chosen_log_path), max_bytes=40_000)
            st.text_area("Backtest log (tail)", value=tail, height=260)

            # Progress bar (best-effort) by parsing backtest JSONL progress records.
            bt_jsonl = _extract_backtest_log_path_from_process_log(tail)
            if bt_jsonl:
                prog = _read_latest_backtest_progress(bt_jsonl)
                if prog:
                    total = int(prog.get("events_total") or 0)
                    scanned = int(prog.get("events_scanned") or 0)
                    simulated = int(prog.get("events_simulated") or 0)
                    pct = int(round(100.0 * scanned / total)) if total > 0 else 0
                    st.progress(min(100, max(0, pct)))
                    st.caption(f"Progress: {scanned}/{total} events scanned ({pct}%), {simulated} simulated. Latest event: `{prog.get('event')}`")
                else:
                    st.caption("Progress: waiting for first `record_type=progress` in backtest JSONL…")
            else:
                st.caption("Progress: waiting for backtest to print its JSONL log path…")

        # Optional: debug panel for when Streamlit UI doesn't match reality.
        with st.expander("Debug backtest status", expanded=False):
            st.json(
                {
                    "backtest_running": bool(backtest_running),
                    "session.backtest_pid": backtest_pid,
                    "session.backtest_log_path": backtest_log_path,
                    "pid_is_backtest": bool(_pid_is_backtest_process(backtest_pid)),
                    "discovered_backtests": [{"pid": pid, "cmd": cmd} for pid, cmd in discovered[:10]],
                    "chosen_log_path": chosen_log_path,
                }
            )

        # Completion / ingestion
        backtest_handle = st.session_state.get("backtest_handle")
        finished = False
        exit_code = None
        if backtest_handle is not None and backtest_handle.popen.poll() is not None:
            finished = True
            exit_code = backtest_handle.popen.returncode
        elif backtest_pid is not None and (not _pid_is_backtest_process(backtest_pid)):
            finished = True
        elif chosen_log_path:
            # If session state was lost, infer completion from the process.log content.
            # backtest_report prints "=== Backtest Summary ===" at the end.
            try:
                if "=== Backtest Summary ===" in (tail or ""):
                    finished = True
            except Exception:
                pass

        if finished:
            msg = "Backtest finished."
            if exit_code is not None:
                msg += f" (exit code {exit_code})"
            st.success(msg)
            st.session_state["run_mode"] = "idle"

            if ingest_after and not st.session_state.get("backtest_ingested", False):
                log_dir = str(st.session_state["config_dict"].get("backtest", {}).get("LOG_DIR") or "backtests")
                latest = _latest_backtest_jsonl(log_dir)
                if latest:
                    from dashboard.ingest.ingest_backtest_jsonl import ingest_backtest_jsonl

                    n = ingest_backtest_jsonl(input_path=latest, db_path=db_path)
                    st.session_state["backtest_ingested"] = True
                    st.success(f"Ingested {n} backtest records from `{latest}` into `{db_path}`.")
                else:
                    st.warning(f"No backtest JSONL found under `{log_dir}` to ingest.")

    # -----------------
    # Trading runner
    # -----------------
    with sub_trading:
        st.markdown("**Run evaluation / paper / live**")
        mode = st.selectbox("Run mode", ["evaluation (no orders)", "paper trading (dry-run)", "live trading"], index=0)
        event_override = st.text_input("Optional: event ticker override (leave blank for auto-discovery)", value="")
        watch = st.checkbox("Watch loop (--watch)", value=True)
        auto_ingest_live = st.checkbox("Auto-ingest this run into dashboard DB", value=True)

        # Optional env overrides (do not persist secrets)
        with st.expander("Auth env overrides (optional; leave blank to use your shell env vars)"):
            api_key_id = st.text_input("KALSHI_API_KEY_ID", value="", type="password")
            private_key_path = st.text_input("KALSHI_PRIVATE_KEY_PATH", value="", type="password")
            kalshi_base_url = st.text_input("KALSHI_BASE_URL", value="")

        confirm_live = False
        if mode == "live trading":
            confirm_live = st.checkbox("I understand this may place real orders", value=False)
            if not confirm_live:
                st.warning("Live trading is locked until you check the confirmation box.")

        trading_handle: Optional[ProcessHandle] = st.session_state.get("trading_handle")
        ingest_stop: Optional[Event] = st.session_state.get("trading_ingest_stop")

        running = trading_handle is not None and trading_handle.popen.poll() is None
        col1, col2 = st.columns([1, 1])
        with col1:
            if running:
                st.info("Trader is running…")
                if st.button("Stop run"):
                    try:
                        if ingest_stop is not None:
                            ingest_stop.set()
                    except Exception:
                        pass
                    stop_process(trading_handle)
                    st.session_state["run_mode"] = "idle"
            else:
                can_start = True
                if mode == "live trading" and not confirm_live:
                    can_start = False
                if st.button("Start run", type="primary", disabled=not can_start):
                    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    kind = "eval" if mode.startswith("evaluation") else ("paper" if mode.startswith("paper") else "live")
                    run_dir = Path(_REPO_ROOT) / ".dashboard" / "runs" / f"{kind}_{run_ts}"
                    run_dir.mkdir(parents=True, exist_ok=True)

                    cfg_path = str(run_dir / "config.json")
                    _write_text(cfg_path, _pretty_json(st.session_state["config_dict"]))

                    trade_log_path = str(run_dir / "trade_log.jsonl")
                    state_path = str(run_dir / "state.json")

                    args = [sys.executable, "-u", "-m", "kalshi_edge", "--config", cfg_path]
                    if watch:
                        args.append("--watch")
                    if event_override.strip():
                        args += ["--event", event_override.strip()]

                    # keep artifacts isolated for runs launched from the dashboard
                    args += ["--trade-log-file", trade_log_path, "--state-file", state_path]

                    if mode.startswith("paper"):
                        args += ["--trade", "--dry-run"]
                        st.session_state["run_mode"] = "paper"
                    elif mode == "live trading":
                        args += ["--trade"]
                        st.session_state["run_mode"] = "live"
                    else:
                        st.session_state["run_mode"] = "idle"

                    env_overrides = {}
                    if api_key_id:
                        env_overrides["KALSHI_API_KEY_ID"] = api_key_id
                    if private_key_path:
                        env_overrides["KALSHI_PRIVATE_KEY_PATH"] = private_key_path
                    if kalshi_base_url:
                        env_overrides["KALSHI_BASE_URL"] = kalshi_base_url

                    h = start_process(
                        name=f"trader_{kind}",
                        args=args,
                        cwd=str(_REPO_ROOT),
                        log_path=str(run_dir / "process.log"),
                        env_overrides=env_overrides or None,
                    )
                    st.session_state["trading_handle"] = h
                    st.session_state["trading_trade_log_path"] = trade_log_path

                    # start tail ingestion thread (stoppable)
                    if auto_ingest_live and (mode.startswith("paper") or mode == "live trading"):
                        from dashboard.control.tail_ingest import tail_and_ingest

                        stop_ev = Event()
                        th = Thread(
                            target=tail_and_ingest,
                            kwargs={
                                "stop": stop_ev,
                                "input_path": trade_log_path,
                                "db_path": db_path,
                                "poll_seconds": 0.5,
                                "from_start": True,
                            },
                            daemon=True,
                        )
                        th.start()
                        st.session_state["trading_ingest_stop"] = stop_ev
                        st.session_state["trading_ingest_thread_started_at"] = time.time()

                    st.success(f"Started `{kind}` run. Logs + state under `{run_dir}`")

        with col2:
            if trading_handle is not None:
                st.caption(f"Log file: `{trading_handle.log_path}` (pid={trading_handle.popen.pid})")
                tail = read_log_tail(trading_handle.log_path, max_bytes=40_000)
                st.text_area("Run log (tail)", value=tail, height=320)

