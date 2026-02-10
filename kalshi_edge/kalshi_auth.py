"""
kalshi_auth.py

Kalshi Trade API authentication helpers (RSA-PSS signatures).

Per Kalshi docs, every authenticated request requires headers:
  - KALSHI-ACCESS-KEY
  - KALSHI-ACCESS-TIMESTAMP (ms)
  - KALSHI-ACCESS-SIGNATURE = base64(RSA-PSS-SHA256(signature of
        f"{timestamp}{METHOD}{path_without_query}"))
"""

from __future__ import annotations

import base64
import datetime
from dataclasses import dataclass
from typing import Dict, Optional

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def now_ms() -> str:
    """Current unix timestamp in milliseconds as a string."""
    return str(int(datetime.datetime.now().timestamp() * 1000))


def load_private_key_from_file(file_path: str) -> rsa.RSAPrivateKey:
    """Load an RSA private key (PEM) from disk."""
    with open(file_path, "rb") as f:
        return serialization.load_pem_private_key(
            f.read(), password=None, backend=default_backend()
        )  # type: ignore[return-value]


def sign_request(private_key: rsa.RSAPrivateKey, timestamp_ms: str, method: str, path: str) -> str:
    """Create Kalshi base64 signature for an HTTP request."""
    method_u = method.upper()
    # Important: strip query params
    path_wo_query = path.split("?", 1)[0]
    message = f"{timestamp_ms}{method_u}{path_wo_query}".encode("utf-8")

    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


@dataclass
class KalshiAuth:
    """Small helper that holds key material and generates auth headers."""

    api_key_id: str
    private_key_path: str
    _private_key: Optional[rsa.RSAPrivateKey] = None

    @property
    def private_key(self) -> rsa.RSAPrivateKey:
        if self._private_key is None:
            self._private_key = load_private_key_from_file(self.private_key_path)
        return self._private_key

    def headers(self, method: str, path: str, timestamp_ms: Optional[str] = None) -> Dict[str, str]:
        ts = timestamp_ms or now_ms()
        sig = sign_request(self.private_key, ts, method, path)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
        }
