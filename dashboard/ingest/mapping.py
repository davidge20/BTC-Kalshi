from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple


_T_STRIKE_RE = re.compile(r"-T(?P<strike>\d+(?:\.\d+)?)$")


def parse_strike_from_market_ticker(market_ticker: Optional[str]) -> Optional[float]:
    if not market_ticker or not isinstance(market_ticker, str):
        return None
    m = _T_STRIKE_RE.search(market_ticker.strip())
    if not m:
        return None
    try:
        return float(m.group("strike"))
    except Exception:
        return None


def as_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def as_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


def json_dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def normalize_market_ticker(payload: Dict[str, Any]) -> Optional[str]:
    mt = payload.get("market_ticker")
    if isinstance(mt, str) and mt:
        return mt
    tkr = payload.get("ticker")
    if isinstance(tkr, str) and tkr:
        return tkr
    return None


def extract_fee_cents(payload: Dict[str, Any]) -> Optional[int]:
    # Common variants across versions
    if "fee_cents" in payload:
        return as_int(payload.get("fee_cents"))
    if "fee_cents_per_contract" in payload:
        return as_int(payload.get("fee_cents_per_contract"))
    if "fee" in payload:
        # Some logs store fee in dollars
        fv = as_float(payload.get("fee"))
        if fv is None:
            return None
        return int(round(fv * 100))
    return None


def extract_p_model_yes(payload: Dict[str, Any]) -> Optional[float]:
    # In v2 logs, `p` is p_yes. In schema v1, `p_yes` exists.
    if "p_yes" in payload:
        return as_float(payload.get("p_yes"))
    if "p" in payload:
        return as_float(payload.get("p"))
    return None


def extract_implied_q_yes(payload: Dict[str, Any]) -> Optional[float]:
    if "implied_q_yes" in payload:
        return as_float(payload.get("implied_q_yes"))
    if "implied_q" in payload:
        return as_float(payload.get("implied_q"))
    if "implied_q_yes_proxy" in payload:
        return as_float(payload.get("implied_q_yes_proxy"))
    return None


def extract_ev(payload: Dict[str, Any]) -> Optional[float]:
    # Variants: ev, EV
    if "ev" in payload:
        return as_float(payload.get("ev"))
    if "EV" in payload:
        return as_float(payload.get("EV"))
    return None


def extract_edge_pp(payload: Dict[str, Any]) -> Optional[float]:
    if "edge_pp" in payload:
        return as_float(payload.get("edge_pp"))
    return None


def extract_price_cents(payload: Dict[str, Any]) -> Optional[int]:
    if "price_cents" in payload:
        return as_int(payload.get("price_cents"))
    if "buy_cents" in payload:
        return as_int(payload.get("buy_cents"))
    if "exit_bid_cents" in payload:
        return as_int(payload.get("exit_bid_cents"))
    if "bid_cents" in payload:
        return as_int(payload.get("bid_cents"))
    return None


def infer_order_key(payload: Dict[str, Any]) -> Optional[str]:
    # Prefer stable ids if available
    oid = payload.get("order_id")
    if isinstance(oid, str) and oid:
        return oid
    coid = payload.get("client_order_id")
    if isinstance(coid, str) and coid:
        return coid
    return None


def fill_kind_for_event(event: str) -> Optional[str]:
    if event == "entry_filled":
        return "entry"
    if event == "scale_in_filled":
        return "scale_in"
    if event == "exit_filled":
        return "exit"
    return None

