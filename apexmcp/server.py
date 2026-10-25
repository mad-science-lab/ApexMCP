"""MCP server entry point (stdio). Write tools are only registered when enabled."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import tools
from .apex import ApexClient
from .config import ConfigError, Settings, load

_LOG = logging.getLogger("apexmcp")


class _Runtime:
    """Holds one ApexClient, opened on first use and closed at shutdown."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: ApexClient | None = None

    async def client(self) -> ApexClient:
        if self._client is None:
            s = self.settings
            self._client = await ApexClient(
                s.host, s.username, s.password, timeout_seconds=s.timeout_seconds
            ).__aenter__()
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None


def build_server(settings: Settings) -> FastMCP:
    rt = _Runtime(settings)

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await rt.close()

    mcp = FastMCP(
        "apex",
        instructions=(
            "Neptune Apex aquarium controller. Read tools are always safe. "
            "Outlet changes are manual overrides of the controller's own programs: "
            "prefer AUTO, and say what you changed. Never guess an outlet name; list outlets first."
        ),
        lifespan=lifespan,
    )

    # -- read ---------------------------------------------------------------

    @mcp.tool()
    async def apex_system_info() -> dict[str, Any]:
        """Controller identity, firmware, modules, feed state, power-failure times, network quality."""
        return await tools.system_info(await rt.client())

    @mcp.tool()
    async def apex_get_probes(probe_type: str | None = None) -> list[dict[str, Any]]:
        """Current readings. probe_type filters, e.g. Temp, pH, Amps, pwr, volts, digital, Cond."""
        return await tools.probes(await rt.client(), probe_type)

    @mcp.tool()
    async def apex_get_outlets(outlet_type: str | None = None) -> list[dict[str, Any]]:
        """Every outlet with mode (AUTO/ON/OFF), whether it is energized, power level and pump mode."""
        return await tools.outlets(await rt.client(), outlet_type)

    @mcp.tool()
    async def apex_feed_status() -> dict[str, Any]:
        """Whether a feed cycle (A to D) is running and how many seconds remain."""
        return await tools.feed_status(await rt.client())

    @mcp.tool()
    async def apex_module_health() -> list[dict[str, Any]]:
        """Aquabus module presence, firmware status and packet error rate."""
        return await tools.module_health(await rt.client())

    @mcp.tool()
    async def apex_get_config(section: str) -> Any:
        """One config section: oconf, iconf, pconf, mconf, dconf, clock, misc, season or cal.
        Credentials are always redacted."""
        return await tools.config_section(await rt.client(), section)

    @mcp.tool()
    async def apex_probe_history(
        probes: list[str] | None = None, days: int = 1, max_points: int = 144
    ) -> dict[str, Any]:
        """Probe history from the controller's log (default Temp and pH), downsampled to max_points."""
        return await tools.probe_history(await rt.client(), probes, days, max_points)

    @mcp.tool()
    async def apex_probe_stats(probe: str, days: int = 7) -> dict[str, Any]:
        """min/mean/max, daily means and day-over-day drift for one probe (name or device id)."""
        return await tools.probe_stats(await rt.client(), probe, days)

    @mcp.tool()
    async def apex_outlet_events(
        outlet: str | None = None, days: int = 1, limit: int = 200
    ) -> dict[str, Any]:
        """Exact outlet on/off times (second resolution). Only outlets with logging enabled appear."""
        return await tools.outlet_events(await rt.client(), outlet, days, limit)

    @mcp.tool()
    async def apex_duty_cycle(outlet: str, days: int = 1) -> dict[str, Any]:
        """Percent of time an outlet was on, run count and mean run length. Good for heaters and ATO pumps."""
        return await tools.duty_cycle(await rt.client(), outlet, days)

    @mcp.tool()
    async def apex_get_program(outlet: str | None = None) -> Any:
        """The Apex program text for one outlet, or all outlets when outlet is omitted."""
        return await tools.outlet_program(await rt.client(), outlet)

    # -- write (only registered when enabled) -----------------------------------

    if settings.allow_write:

        @mcp.tool()
        async def apex_set_outlet(outlet: str, mode: str) -> dict[str, Any]:
            """Set an outlet to AUTO, ON or OFF. ON/OFF override the program until set back to AUTO.
            Protected outlets only accept AUTO. Returns the state before and after."""
            return await tools.set_outlet(await rt.client(), settings, outlet, mode)

        @mcp.tool()
        async def apex_start_feed(cycle: str) -> dict[str, Any]:
            """Start feed cycle A, B, C or D."""
            return await tools.start_feed(await rt.client(), settings, cycle)

        @mcp.tool()
        async def apex_cancel_feed() -> dict[str, Any]:
            """Cancel the running feed cycle. Ignored when none is running."""
            return await tools.cancel_feed(await rt.client(), settings)

    if settings.allow_program_write:

        @mcp.tool()
        async def apex_update_program(
            outlet: str, program: str, confirm: bool = False
        ) -> dict[str, Any]:
            """Replace an outlet's program. Without confirm=true it only returns a diff.
            With confirm=true the old program is backed up to disk, then written."""
            return await tools.update_program(await rt.client(), settings, outlet, program, confirm)

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = load()
    except ConfigError as exc:
        print(f"apexmcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    _LOG.info(
        "apexmcp starting for %s (writes %s, program edits %s)",
        settings.host,
        "ON" if settings.allow_write else "off",
        "ON" if settings.allow_program_write else "off",
    )
    build_server(settings).run()


if __name__ == "__main__":
    main()
