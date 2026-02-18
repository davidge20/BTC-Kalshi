"""
http_client.py

@brief 
- Fail fast: non-2xx responses raise, and non-JSON responses error at parse time.
  This keeps call sites simple and forces upstream handling to be explicit.
- No hidden state: uses one-off `requests.*` calls (no shared Session) to avoid
  surprising cross-request coupling; if you need connection pooling, add a Session.
"""

from __future__ import annotations
from typing import Optional, Dict, Any
import requests


class HttpClient:
    """Small wrapper around `requests` for JSON APIs.
    """

    def __init__(self, debug: bool = False, timeout: int = 15):
        """Create a client.
        """
        self.debug = debug
        self.timeout = timeout

    def get_json(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> Dict[str, Any]:
        """HTTP GET that returns parsed JSON.
        """
        if self.debug:
            print(f"[HTTP] GET {url}")
            if params:
                print(f"params={params}")

        r = requests.get(url, params=params, headers=headers, timeout=self.timeout)

        if self.debug:
            print(f"status={r.status_code} bytes={len(r.content)}")

        r.raise_for_status()
        data = r.json()

        if self.debug and isinstance(data, dict):
            print(f"keys={list(data.keys())[:12]}")

        return data

    def post_json(
        self,
        url: str,
        json_body: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """HTTP POST with JSON body; returns parsed JSON.
        """
        if self.debug:
            print(f"[HTTP] POST {url}")

        r = requests.post(url, headers=headers, json=json_body, timeout=self.timeout)

        if self.debug:
            print(f"status={r.status_code} bytes={len(r.content)}")
            if r.status_code >= 400:
                # Prefix-only: response bodies can be huge
                print(f"error_body={r.text[:400]}")

        r.raise_for_status()
        return r.json()

    def request_json(
        self,
        method: str,
        url: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """Generic JSON request for non-GET verbs (PUT/PATCH/DELETE/etc)."""
        m = method.upper()
        if self.debug:
            print(f"[HTTP] {m} {url}")
            if params:
                print(f"      params={params}")

        r = requests.request(m, url, params=params, headers=headers, json=json_body, timeout=self.timeout)

        if self.debug:
            print(f"status={r.status_code} bytes={len(r.content)}")
            if r.status_code >= 400:
                # Keep debug output bounded and readable.
                print(f"error_body={r.text[:400]}")

        r.raise_for_status()
        return r.json()
