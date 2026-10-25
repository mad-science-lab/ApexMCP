"""Strip credentials before anything leaves the process.

`/rest/config` carries the controller admin password in plaintext under `nconf`,
and `/rest/status` carries a Fusion `linkKey`. Neither should reach a model.
"""

from __future__ import annotations

from typing import Any

REDACTED = "[redacted]"
_SECRET_KEYS = {
    "nconf", "password", "passwd", "pass", "pwd", "linkkey", "token",
    "apikey", "api_key", "secret", "cookie", "psk", "wifipass",
}


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: REDACTED if str(k).lower() in _SECRET_KEYS else redact(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj
