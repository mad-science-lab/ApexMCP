"""Async client for the Neptune Apex local REST API (AOS 5.x).

Session-cookie auth with one automatic re-login on 401. A 429 surfaces as
`ApexRateLimited`. /rest/status is cached for a few seconds so a burst of tool
calls does not hammer a controller that rate-limits.

Ported from ReefHub's client; adds config, history and program access.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import aiohttp

_LOG = logging.getLogger("apexmcp.apex")

_HEADERS = {
    "Accept-Version": "1",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/json",
}

STATUS_CACHE_SECONDS = 3.0


class ApexAuthError(RuntimeError):
    """Login failed (bad credentials)."""


class ApexRateLimited(RuntimeError):
    """Controller returned 429. Back off for `retry_after` seconds."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after:.1f}s")
        self.retry_after = retry_after


class ApexClient:
    def __init__(
        self, host: str, username: str, password: str, *, timeout_seconds: int = 10
    ) -> None:
        self._host = host.rstrip("/")
        if "://" not in self._host:
            self._host = f"http://{self._host}"
        self._username = username
        self._password = password
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._logged_in = False
        self._status: tuple[float, dict[str, Any]] | None = None

    async def __aenter__(self) -> ApexClient:
        self._session = aiohttp.ClientSession(
            headers=_HEADERS,
            timeout=self._timeout,
            # Controllers are often addressed by IP; without unsafe=True aiohttp
            # drops the session cookie. Harmless for hostnames like reef.local.
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _login(self) -> None:
        assert self._session is not None
        async with self._lock:
            _LOG.debug("POST /rest/login as %s", self._username)
            async with self._session.post(
                f"{self._host}/rest/login",
                json={"login": self._username, "password": self._password},
            ) as resp:
                if resp.status in (401, 403):
                    raise ApexAuthError(f"login rejected: HTTP {resp.status}")
                if resp.status == 429:
                    raise ApexRateLimited(_retry_after(resp))
                resp.raise_for_status()
                self._logged_in = True

    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        retry_login: bool = True,
    ) -> Any:
        assert self._session is not None
        if not self._logged_in:
            await self._login()
        async with self._session.request(method, f"{self._host}{path}", json=body) as resp:
            if resp.status == 401 and retry_login:
                _LOG.info("401 on %s %s - logging in again", method, path)
                self._logged_in = False
                await self._login()
                return await self._request(method, path, body, retry_login=False)
            if resp.status == 429:
                raise ApexRateLimited(_retry_after(resp))
            resp.raise_for_status()
            return await resp.json(content_type=None)

    @staticmethod
    def _bust() -> str:
        return f"_={int(time.time() * 1000)}"

    # -- read ---------------------------------------------------------------

    async def get_status(self, *, fresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not fresh and self._status and now - self._status[0] < STATUS_CACHE_SECONDS:
            return self._status[1]
        data = await self._request("GET", f"/rest/status?{self._bust()}")
        self._status = (time.monotonic(), data)
        return data

    async def get_config(self) -> dict[str, Any]:
        """Full config. Contains `nconf` (credentials): redact before returning to a model."""
        return await self._request("GET", f"/rest/config?{self._bust()}")

    async def get_ilog(self, days: int, sdate: str) -> dict[str, Any]:
        """Input history (probes, power). `sdate` is YYMMDD and is the START date; the log
        runs from there to sdate + days, clipped at now (verified on AOS 5.16L)."""
        return await self._request("GET", f"/rest/ilog?days={days}&sdate={sdate}&{self._bust()}")

    async def get_olog(self, days: int, sdate: str) -> Any:
        """Outlet on/off events with second resolution. `sdate` is YYMMDD and is the START date."""
        return await self._request("GET", f"/rest/olog?days={days}&sdate={sdate}&{self._bust()}")

    # -- write --------------------------------------------------------------

    async def set_outlet(self, did: str, mode: str) -> None:
        """mode is AUTO, ON or OFF. Does not touch the outlet's program."""
        mode = mode.upper()
        if mode not in ("AUTO", "ON", "OFF"):
            raise ValueError(f"invalid outlet mode: {mode!r}")
        await self._request(
            "PUT",
            f"/rest/status/outputs/{did}",
            {"did": did, "status": [mode, "", "OK", ""], "type": "outlet"},
        )
        self._status = None

    async def start_feed(self, cycle: int) -> None:
        """cycle 1..4 = Feed A..D."""
        if cycle not in (1, 2, 3, 4):
            raise ValueError(f"invalid feed cycle: {cycle}")
        await self._request(
            "PUT",
            f"/rest/status/feed/{cycle}",
            {"active": 1, "errorCode": 0, "errorMessage": "", "name": cycle},
        )
        self._status = None

    async def cancel_feed(self) -> None:
        await self._request(
            "PUT",
            "/rest/status/feed/0",
            {"active": 1, "errorCode": 0, "errorMessage": "", "name": 0},
        )
        self._status = None

    async def put_outlet_config(self, outlet: dict[str, Any]) -> Any:
        """Write a COMPLETE outlet config object (no partial updates). Takes effect immediately."""
        did = str(outlet["did"])
        return await self._request("PUT", f"/rest/config/oconf/{did}", outlet)


def _retry_after(resp: aiohttp.ClientResponse) -> float:
    header = resp.headers.get("Retry-After")
    try:
        if header:
            return float(header) + random.uniform(0.0, 1.0)
    except ValueError:
        pass
    return 30.0 + random.uniform(0.0, 5.0)
