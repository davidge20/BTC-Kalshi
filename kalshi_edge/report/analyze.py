"""
report/analyze.py

Compute realized (hold-to-expiry) PnL for a kalshi_edge run JSONL by:
- reading fills from the log
- inferring side+fee from nearby order_submit/decision records
- fetching settlement outcomes from Kalshi (live or historical endpoints)
- computing payout and PnL per fill, aggregated by market + event

Usage:
  python3 -m kalshi_edge.report.analyze run.jsonl --outdir out --cache markets_cache.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
import requests
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from kalshi_edge.constants import KALSHI

BASE = KALSHI


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def event_from_market_ticker(market_ticker: str) -> str:
    return market_ticker.split("-T", 1)[0] if "-T" in market_ticker else market_ticker


@dataclass
class Fill:
    fill_ts: datetime
    market_ticker: str
    fill_count: int
    fill_price_cents: int
    side: Optional[str] = None
    fee_cents: int = 0
    submit_ts: Optional[datetime] = None


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if "ts_utc" in r:
                r["_ts"] = parse_ts(r["ts_utc"])
            out.append(r)
    out.sort(key=lambda r: r.get("_ts") or datetime.min)
    return out


def write_csv(path: str, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fieldnames})


def fetch_market_any(ticker: str, session: "requests.Session", timeout: int = 15) -> Dict[str, Any]:
    live_url = f"{BASE}/markets/{ticker}"
    hist_url = f"{BASE}/historical/markets/{ticker}"

    r = session.get(live_url, timeout=timeout)
    if r.status_code == 200:
        return r.json()
    if r.status_code == 404:
        rh = session.get(hist_url, timeout=timeout)
        rh.raise_for_status()
        return rh.json()

    r.raise_for_status()
    return r.json()


def infer_side_and_fee(
    records: List[Dict[str, Any]],
    fills: List[Fill],
    max_lookback_s: float = 60 * 30,
) -> None:
    submits_by_market: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    decisions_by_key: Dict[Tuple[str, str, int, int], List[Dict[str, Any]]] = defaultdict(list)

    for r in records:
        ev = r.get("event")
        if ev == "order_submit":
            mt = r.get("market_ticker")
            if isinstance(mt, str):
                submits_by_market[mt].append(r)
        elif ev == "decision" and str(r.get("action", "")).lower() == "submit":
            mt = r.get("market_ticker")
            side = str(r.get("side", "")).lower()
            price = r.get("price_cents")
            count = r.get("count")
            if isinstance(mt, str) and side in ("yes", "no") and isinstance(price, int) and isinstance(count, int):
                decisions_by_key[(mt, side, price, count)].append(r)

    for mt in submits_by_market:
        submits_by_market[mt].sort(key=lambda x: x["_ts"])
    for k in decisions_by_key:
        decisions_by_key[k].sort(key=lambda x: x["_ts"])

    for f in fills:
        subs = submits_by_market.get(f.market_ticker, [])
        if not subs:
            continue

        best = None
        best_dt = None
        for s in subs:
            if s.get("price_cents") != f.fill_price_cents:
                continue
            if s.get("count") != f.fill_count:
                continue
            ts = s["_ts"]
            if ts > f.fill_ts:
                continue
            dt = (f.fill_ts - ts).total_seconds()
            if dt < 0 or dt > max_lookback_s:
                continue
            if best is None or dt < best_dt:
                best, best_dt = s, dt

        if not best:
            continue

        f.submit_ts = best["_ts"]
        f.side = str(best.get("side", "")).lower() if best.get("side") else None

        if f.side in ("yes", "no"):
            key = (f.market_ticker, f.side, f.fill_price_cents, f.fill_count)
            cand = decisions_by_key.get(key, [])
            fee = 0
            if cand:
                tgt = f.submit_ts or f.fill_ts
                chosen = min(cand, key=lambda d: abs((d["_ts"] - tgt).total_seconds()))
                fc = chosen.get("fee_cents")
                if isinstance(fc, int):
                    fee = fc
            f.fee_cents = fee


def payout_cents_for_side(result: str, side: str, settlement_value: Optional[int]) -> Optional[int]:
    """
    For binary markets:
      result == "yes" => YES pays 100, NO pays 0
      result == "no"  => NO pays 100, YES pays 0
    For 'scalar' markets, use settlement_value if present.
    For 'void', positions are generally returned at cost basis.
    """
    result = (result or "").lower()
    side = (side or "").lower()

    if result in ("yes", "no"):
        return 100 if result == side else 0

    if result == "scalar":
        return settlement_value

    if result == "void":
        return None

    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="kalshi_edge.report.analyze",
        description="Compute realized PnL from a kalshi_edge JSONL log.",
    )
    ap.add_argument("jsonl_path")
    ap.add_argument("--outdir", default="pnl_out")
    ap.add_argument("--cache", default="markets_cache.json", help="Cache market fetches here")
    ap.add_argument("--sleep-ms", type=int, default=50, help="Sleep between API calls")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    records = read_jsonl(args.jsonl_path)

    fills: List[Fill] = []
    for r in records:
        if r.get("event") != "paper_fill":
            continue
        mt = r.get("market_ticker")
        if not isinstance(mt, str):
            continue
        fills.append(
            Fill(
                fill_ts=r["_ts"],
                market_ticker=mt,
                fill_count=int(r.get("fill_count") or 0),
                fill_price_cents=int(r.get("fill_price_cents") or 0),
            )
        )

    infer_side_and_fee(records, fills)

    cache_path = args.cache
    market_cache: Dict[str, Any] = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                market_cache = json.load(f)
        except Exception:
            market_cache = {}

    import requests
    unique_markets = sorted({f.market_ticker for f in fills})
    sess = requests.Session()

    for tkr in unique_markets:
        if tkr in market_cache:
            continue
        try:
            data = fetch_market_any(tkr, sess)
            market_cache[tkr] = data
            if args.debug:
                m = data.get("market", {})
                print(f"[market] {tkr} status={m.get('status')} result={m.get('result')} settlement_ts={m.get('settlement_ts')}")
        except Exception as e:
            market_cache[tkr] = {"_error": str(e)}
            if args.debug:
                print(f"[market] {tkr} ERROR: {e}")
        time.sleep(max(0.0, args.sleep_ms / 1000.0))

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(market_cache, f, indent=2, sort_keys=True)

    fill_rows: List[Dict[str, Any]] = []
    market_rows: Dict[Tuple[str, str], Dict[str, Any]] = {}
    event_rows: Dict[str, Dict[str, Any]] = {}

    total_pnl = 0.0
    total_cost = 0.0
    unresolved = 0

    for f in fills:
        side = f.side or "UNKNOWN"
        fee = int(f.fee_cents or 0)

        cost_cents = (f.fill_price_cents + fee) * f.fill_count
        cost_usd = cost_cents / 100.0

        data = market_cache.get(f.market_ticker, {})
        m = data.get("market", {}) if isinstance(data, dict) else {}
        status = (m.get("status") or "").lower()
        result = (m.get("result") or "").lower()
        settlement_value = m.get("settlement_value")
        settlement_value = int(settlement_value) if isinstance(settlement_value, int) else None
        settlement_ts = m.get("settlement_ts")

        pnl_usd = None
        payout_cents = None

        is_settled = (
            status in ("settled", "finalized")
            or m.get("settlement_ts") is not None
            or (m.get("result") or "").lower() in ("yes", "no", "scalar", "void")
        )

        if side in ("yes", "no") and is_settled:
            pc = payout_cents_for_side(result, side, settlement_value)
            if pc is not None:
                payout_cents = pc * f.fill_count
                pnl_cents = payout_cents - cost_cents
                pnl_usd = pnl_cents / 100.0
            else:
                if result == "void":
                    payout_cents = f.fill_price_cents * f.fill_count
                    pnl_cents = payout_cents - cost_cents
                    pnl_usd = pnl_cents / 100.0

        if pnl_usd is None:
            unresolved += 1
        else:
            total_pnl += pnl_usd

        total_cost += cost_usd

        et = event_from_market_ticker(f.market_ticker)
        fill_rows.append(
            {
                "fill_ts": f.fill_ts.isoformat(),
                "event_ticker": et,
                "market_ticker": f.market_ticker,
                "side": side,
                "count": f.fill_count,
                "fill_price_cents": f.fill_price_cents,
                "fee_cents": fee,
                "cost_usd": round(cost_usd, 6),
                "market_status": status,
                "market_result": result,
                "settlement_value": settlement_value,
                "settlement_ts": settlement_ts,
                "payout_cents_total": payout_cents,
                "pnl_usd": None if pnl_usd is None else round(pnl_usd, 6),
            }
        )

        key = (f.market_ticker, side)
        if key not in market_rows:
            market_rows[key] = {
                "event_ticker": et,
                "market_ticker": f.market_ticker,
                "side": side,
                "contracts": 0,
                "cost_usd": 0.0,
                "pnl_usd": 0.0,
                "settled_contracts": 0,
                "market_status": status,
                "market_result": result,
            }
        market_rows[key]["contracts"] += f.fill_count
        market_rows[key]["cost_usd"] += cost_usd
        if pnl_usd is not None:
            market_rows[key]["pnl_usd"] += pnl_usd
            market_rows[key]["settled_contracts"] += f.fill_count

        if et not in event_rows:
            event_rows[et] = {"event_ticker": et, "contracts": 0, "cost_usd": 0.0, "pnl_usd": 0.0, "settled_contracts": 0}
        event_rows[et]["contracts"] += f.fill_count
        event_rows[et]["cost_usd"] += cost_usd
        if pnl_usd is not None:
            event_rows[et]["pnl_usd"] += pnl_usd
            event_rows[et]["settled_contracts"] += f.fill_count

    os.makedirs(args.outdir, exist_ok=True)
    write_csv(
        os.path.join(args.outdir, "fills_with_settlement.csv"),
        fill_rows,
        ["fill_ts", "event_ticker", "market_ticker", "side", "count", "fill_price_cents",
         "fee_cents", "cost_usd", "market_status", "market_result", "settlement_value",
         "settlement_ts", "payout_cents_total", "pnl_usd"],
    )

    market_summary = list(market_rows.values())
    market_summary.sort(key=lambda r: (r["event_ticker"], r["market_ticker"], r["side"]))
    write_csv(
        os.path.join(args.outdir, "market_summary.csv"),
        market_summary,
        ["event_ticker", "market_ticker", "side", "contracts", "settled_contracts", "cost_usd", "pnl_usd", "market_status", "market_result"],
    )

    event_summary = list(event_rows.values())
    event_summary.sort(key=lambda r: r["event_ticker"])
    write_csv(
        os.path.join(args.outdir, "event_summary.csv"),
        event_summary,
        ["event_ticker", "contracts", "settled_contracts", "cost_usd", "pnl_usd"],
    )

    print("=== Settlement PnL Summary ===")
    print(f"fills:             {len(fills)}")
    print(f"total cost ($):    {total_cost:.4f}")
    print(f"realized pnl ($):  {total_pnl:.4f}")
    if total_cost > 0:
        print(f"ROI (pnl/cost):    {total_pnl/total_cost:.4%}")
    print(f"unresolved fills:  {unresolved}  (markets not settled yet or missing side/result)")
    print(f"wrote: {args.outdir}/fills_with_settlement.csv")
    print(f"      {args.outdir}/market_summary.csv")
    print(f"      {args.outdir}/event_summary.csv")
    print(f"cache: {cache_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
