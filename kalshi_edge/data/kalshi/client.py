"""
data/kalshi/client.py — Kalshi REST API functions.

Public (unauthenticated):
  get_event, get_orderbook

Authenticated portfolio operations:
  create_order, get_order, get_orders, cancel_order, amend_order, get_positions
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol

from kalshi_edge.constants import KALSHI


class HttpClientLike(Protocol):
    def get_json(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> Dict[str, Any]: ...
    def post_json(self, url: str, json_body: Optional[dict] = None, headers: Optional[dict] = None) -> Dict[str, Any]: ...
    def request_json(
        self,
        method: str,
        url: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> Dict[str, Any]: ...


class KalshiAuthLike(Protocol):
    """Structural type for objects that can generate Kalshi auth headers."""
    def headers(self, method: str, path: str, timestamp_ms: Optional[str] = None) -> Dict[str, str]: ...


# ---------------------------------------------------------------------------
# Public (unauthenticated) market data
# ---------------------------------------------------------------------------

def get_event(http: HttpClientLike, event_ticker: str) -> Dict[str, Any]:
    """Fetch an event including nested markets."""
    return http.get_json(f"{KALSHI}/events/{event_ticker}", params={"with_nested_markets": "true"})


def get_orderbook(http: HttpClientLike, market_ticker: str) -> Dict[str, Any]:
    """Fetch a market's orderbook (YES bids + NO bids)."""
    return http.get_json(f"{KALSHI}/markets/{market_ticker}/orderbook")


# ---------------------------------------------------------------------------
# Authenticated portfolio operations
# ---------------------------------------------------------------------------

def create_order(
    http: HttpClientLike,
    auth: KalshiAuthLike,
    order_data: Dict[str, Any],
    *,
    base_url: str = KALSHI,
    subaccount: Optional[int] = None,
) -> Dict[str, Any]:
    """Create an order (authenticated)."""
    path = "/trade-api/v2/portfolio/orders"
    headers = auth.headers("POST", path)
    headers["Content-Type"] = "application/json"
    params: Dict[str, Any] = {}
    if subaccount is not None:
        params["subaccount"] = int(subaccount)
    url = f"{base_url}/portfolio/orders"
    if params:
        return http.request_json("POST", url, params=params, headers=headers, json_body=order_data)
    return http.post_json(url, json_body=order_data, headers=headers)


def get_order(
    http: HttpClientLike,
    auth: KalshiAuthLike,
    order_id: str,
    *,
    base_url: str = KALSHI,
    subaccount: Optional[int] = None,
) -> Dict[str, Any]:
    """Get one order by id (authenticated)."""
    order_id = str(order_id)
    path = f"/trade-api/v2/portfolio/orders/{order_id}"
    headers = auth.headers("GET", path)
    params: Dict[str, Any] = {}
    if subaccount is not None:
        params["subaccount"] = int(subaccount)
    return http.get_json(f"{base_url}/portfolio/orders/{order_id}", params=params or None, headers=headers)


def get_orders(
    http: HttpClientLike,
    auth: KalshiAuthLike,
    *,
    base_url: str = KALSHI,
    status: Optional[str] = None,
    ticker: Optional[str] = None,
    event_ticker: Optional[str] = None,
    limit: int = 200,
    cursor: Optional[str] = None,
    subaccount: Optional[int] = None,
) -> Dict[str, Any]:
    """List portfolio orders (authenticated)."""
    path = "/trade-api/v2/portfolio/orders"
    headers = auth.headers("GET", path)
    params: Dict[str, Any] = {"limit": int(limit)}
    if status:
        params["status"] = str(status)
    if ticker:
        params["ticker"] = str(ticker)
    if event_ticker:
        params["event_ticker"] = str(event_ticker)
    if cursor:
        params["cursor"] = str(cursor)
    if subaccount is not None:
        params["subaccount"] = int(subaccount)
    return http.get_json(f"{base_url}/portfolio/orders", params=params, headers=headers)


def cancel_order(
    http: HttpClientLike,
    auth: KalshiAuthLike,
    order_id: str,
    *,
    base_url: str = KALSHI,
    subaccount: Optional[int] = None,
) -> Dict[str, Any]:
    """Cancel one order (authenticated)."""
    order_id = str(order_id)
    path = f"/trade-api/v2/portfolio/orders/{order_id}"
    headers = auth.headers("DELETE", path)
    params: Dict[str, Any] = {}
    if subaccount is not None:
        params["subaccount"] = int(subaccount)
    return http.request_json("DELETE", f"{base_url}/portfolio/orders/{order_id}", params=params or None, headers=headers)


def amend_order(
    http: HttpClientLike,
    auth: KalshiAuthLike,
    order_id: str,
    amend_data: Dict[str, Any],
    *,
    base_url: str = KALSHI,
    subaccount: Optional[int] = None,
) -> Dict[str, Any]:
    """Amend an order (price and/or max fillable contracts)."""
    order_id = str(order_id)
    path = f"/trade-api/v2/portfolio/orders/{order_id}/amend"
    headers = auth.headers("POST", path)
    headers["Content-Type"] = "application/json"
    params: Dict[str, Any] = {}
    if subaccount is not None:
        params["subaccount"] = int(subaccount)
    return http.request_json(
        "POST",
        f"{base_url}/portfolio/orders/{order_id}/amend",
        params=params or None,
        headers=headers,
        json_body=amend_data,
    )


def get_positions(
    http: HttpClientLike,
    auth: KalshiAuthLike,
    *,
    base_url: str = KALSHI,
    ticker: Optional[str] = None,
    event_ticker: Optional[str] = None,
    limit: int = 1000,
    cursor: Optional[str] = None,
    count_filter: Optional[str] = "position",
    subaccount: Optional[int] = None,
) -> Dict[str, Any]:
    """Get portfolio positions (authenticated)."""
    path = "/trade-api/v2/portfolio/positions"
    headers = auth.headers("GET", path)
    params: Dict[str, Any] = {"limit": int(limit)}
    if ticker:
        params["ticker"] = ticker
    if event_ticker:
        params["event_ticker"] = event_ticker
    if cursor:
        params["cursor"] = cursor
    if count_filter:
        params["count_filter"] = count_filter
    if subaccount is not None:
        params["subaccount"] = int(subaccount)
    return http.get_json(f"{base_url}/portfolio/positions", params=params, headers=headers)
