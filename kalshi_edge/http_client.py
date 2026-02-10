"""http_client.py

Minimal wrapper around requests that:
  - supports debug logging
  - returns parsed JSON
  - raises for non-2xx responses
"""

from __future__ import annotations
from typing import Optional, Dict, Any
import requests


class HttpClient:
    def __init__(self, debug: bool = False, timeout: int = 15):
        self.debug = debug
        self.timeout = timeout

    def get_json(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> Dict[str, Any]:
        if self.debug:
            print(f"[HTTP] GET {url}")
            if params:
                print(f"      params={params}")

        r = requests.get(url, params=params, headers=headers, timeout=self.timeout)

        if self.debug:
            print(f"      status={r.status_code} bytes={len(r.content)}")

        r.raise_for_status()
        data = r.json()

        if self.debug and isinstance(data, dict):
            print(f"      keys={list(data.keys())[:12]}")

        return data

    def post_json(
        self,
        url: str,
        json_body: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """HTTP POST with JSON body; returns parsed JSON."""
        if self.debug:
            print(f"[HTTP] POST {url}")

        r = requests.post(url, headers=headers, json=json_body, timeout=self.timeout)

        if self.debug:
            print(f"      status={r.status_code} bytes={len(r.content)}")
            if r.status_code >= 400:
                print(f"      error_body={r.text[:400]}")

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
        """Generic method when you need non-GET verbs."""
        m = method.upper()
        if self.debug:
            print(f"[HTTP] {m} {url}")
            if params:
                print(f"      params={params}")

        r = requests.request(m, url, params=params, headers=headers, json=json_body, timeout=self.timeout)

        if self.debug:
            print(f"      status={r.status_code} bytes={len(r.content)}")
            if r.status_code >= 400:
                print(f"      error_body={r.text[:400]}")

        r.raise_for_status()
        return r.json()
