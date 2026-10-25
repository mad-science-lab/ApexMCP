"""Tool logic, independent of the MCP framework so it can be tested with a fake client.

Every function takes the Apex client (and, for writes, the Settings) and returns
plain JSON-serialisable data. Anything that changes the controller checks
`Settings` itself, so a mis-registered tool still cannot write.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any, Protocol

from .config import Settings
from .model import Outlet, Snapshot, parse_status
from .redact import redact

POST_WRITE_DELAY = 1.0  # the Apex needs about a second before status reflects a command
CONFIG_SECTIONS = ("oconf", "iconf", "pconf", "mconf", "dconf", "clock", "misc", "season", "cal")
_DIRECTIVES = ("fallback", "set", "if", "defer", "osc", "when", "min", "tdata")
_ON_STATES = {"ON", "AON", "TBL"}
_FEED = {"A": 1, "B": 2, "C": 3, "D": 4}


class ToolError(RuntimeError):
    """A problem to report to the user, not a crash."""


class ApexLike(Protocol):
    async def get_status(self, *, fresh: bool = False) -> dict[str, Any]: ...
    async def get_config(self) -> dict[str, Any]: ...
    async def get_ilog(self, days: int, sdate: str) -> dict[str, Any]: ...
    async def get_olog(self, days: int, sdate: str) -> Any: ...
    async def set_outlet(self, did: str, mode: str) -> None: ...
    async def start_feed(self, cycle: int) -> None: ...
    async def cancel_feed(self) -> None: ...
    async def put_outlet_config(self, outlet: dict[str, Any]) -> Any: ...


# -- helpers ----------------------------------------------------------------


async def _snapshot(client: ApexLike, *, fresh: bool = False) -> Snapshot:
    return parse_status(await client.get_status(fresh=fresh))


def _match(items: list[Any], ref: str) -> Any | None:
    ref_l = ref.strip().lower()
    for it in items:
        if it.did.lower() == ref_l:
            return it
    for it in items:
        if it.name.lower() == ref_l:
            return it
    return None


async def _find_outlet(client: ApexLike, ref: str, *, fresh: bool = False) -> Outlet:
    snap = await _snapshot(client, fresh=fresh)
    outlet = _match(snap.outlets, ref)
    if outlet is None:
        names = ", ".join(f"{o.name} ({o.did})" for o in snap.outlets)
        raise ToolError(f"no outlet matching {ref!r}. Known outlets: {names}")
    return outlet


def _require_write(settings: Settings) -> None:
    if not settings.allow_write:
        raise ToolError("writes are disabled. Set APEX_ALLOW_WRITE=true to enable them.")


def _yymmdd(day: datetime) -> str:
    return day.strftime("%y%m%d")


def _find_records(obj: Any) -> list[dict[str, Any]]:
    """The log envelopes vary, so take the first list of objects found, `record` first."""
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        if "record" in obj:
            return _find_records(obj["record"])
        for v in obj.values():
            found = _find_records(v)
            if found:
                return found
    return []


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# -- read tools -------------------------------------------------------------


async def system_info(client: ApexLike) -> dict[str, Any]:
    raw = await client.get_status()
    snap = parse_status(raw)
    nstat = raw.get("nstat") or {}
    return redact({
        "hostname": snap.hostname,
        "serial": snap.serial,
        "type": snap.system_type,
        "software": snap.software,
        "hardware": snap.hardware,
        "modules": {str(k): v for k, v in snap.modules.items()},
        "feed": snap.feed.to_dict(),
        "power": raw.get("power"),
        "network": {k: nstat.get(k) for k in ("ipaddr", "quality", "strength", "link")},
    })


async def probes(client: ApexLike, probe_type: str | None = None) -> list[dict[str, Any]]:
    snap = await _snapshot(client)
    wanted = probe_type.lower() if probe_type else None
    return [p.to_dict() for p in snap.probes if wanted is None or p.type.lower() == wanted]


async def outlets(client: ApexLike, outlet_type: str | None = None) -> list[dict[str, Any]]:
    snap = await _snapshot(client)
    wanted = outlet_type.lower() if outlet_type else None
    return [o.to_dict() for o in snap.outlets
            if wanted is None or o.type.lower().startswith(wanted)]


async def feed_status(client: ApexLike) -> dict[str, Any]:
    return (await _snapshot(client)).feed.to_dict()


async def module_health(client: ApexLike) -> list[dict[str, Any]]:
    raw = await client.get_status()
    out = []
    for m in raw.get("modules") or []:
        if not isinstance(m, dict):
            continue
        pcount, perror = m.get("pcount") or 0, m.get("perror") or 0
        out.append({
            "abaddr": m.get("abaddr"),
            "hwtype": m.get("hwtype"),
            "present": m.get("present"),
            "swstat": m.get("swstat"),
            "swrev": m.get("swrev"),
            "packets": pcount,
            "errors": perror,
            "error_pct": round(perror / pcount * 100, 3) if pcount else 0.0,
        })
    return out


async def config_section(client: ApexLike, section: str) -> Any:
    if section not in CONFIG_SECTIONS:
        raise ToolError(f"section must be one of: {', '.join(CONFIG_SECTIONS)}")
    cfg = await client.get_config()
    return redact(cfg.get(section))


async def _ilog_records(client: ApexLike, days: int) -> tuple[list[dict[str, Any]], float]:
    days = max(1, min(days, 30))
    # Verified live (AOS 5.16L): ilog `sdate` is the START date and the log runs from
    # there to sdate + days, clipped at now. The older notes said "end date", which
    # returns only today for any `days`.
    start = datetime.now() - timedelta(days=days - 1)
    resp = await client.get_ilog(days, _yymmdd(start))
    tz = _float((resp.get("ilog") or resp).get("timezone")) if isinstance(resp, dict) else None
    return _find_records(resp), (tz or 0.0)


def _wanted(entry: dict[str, Any], refs: list[str] | None) -> bool:
    if not refs:
        return entry.get("type") in ("Temp", "pH")
    low = {r.lower() for r in refs}
    return str(entry.get("name", "")).lower() in low or str(entry.get("did", "")).lower() in low


async def probe_history(
    client: ApexLike, probe_refs: list[str] | None = None, days: int = 1, max_points: int = 144
) -> dict[str, Any]:
    """Probe history from the controller's own log. Defaults to Temp and pH."""
    records, _tz = await _ilog_records(client, days)
    series: dict[str, list[tuple[float, float]]] = {}
    for rec in records:
        ts = _float(rec.get("date"))
        if ts is None:
            continue
        for entry in rec.get("data") or []:
            val = _float(entry.get("value"))
            if val is not None and _wanted(entry, probe_refs):
                series.setdefault(str(entry.get("name")), []).append((ts, val))
    if not series:
        return {"points": [], "note": "no records. Check probe names, or that the log interval has data."}
    stride = max(1, -(-max(len(v) for v in series.values()) // max(1, max_points)))
    by_time: dict[float, dict[str, Any]] = {}
    for name, pts in series.items():
        for ts, val in pts[::stride]:
            by_time.setdefault(ts, {"t": _iso(ts)})[name] = val
    return {"probes": sorted(series), "points": [by_time[t] for t in sorted(by_time)]}


def _day_key(ts: float, tz_hours: float) -> int:
    return int((ts + tz_hours * 3600) // 86400)


async def probe_stats(client: ApexLike, probe: str, days: int = 7) -> dict[str, Any]:
    """min/mean/max, per-day means and day-over-day drift for one probe.

    Drift compares whole-day means, so the daily swing cancels. A straight-line fit
    over a window that is not a whole number of cycles reports phantom drift.
    """
    records, tz = await _ilog_records(client, days)
    values: list[tuple[float, float]] = []
    name = probe
    for rec in records:
        ts = _float(rec.get("date"))
        for entry in rec.get("data") or []:
            val = _float(entry.get("value"))
            if ts is not None and val is not None and _wanted(entry, [probe]):
                values.append((ts, val))
                name = str(entry.get("name"))
    if not values:
        raise ToolError(f"no history for probe {probe!r}")
    per_day: dict[int, list[float]] = {}
    for ts, val in values:
        per_day.setdefault(_day_key(ts, tz), []).append(val)
    daily = [(k, mean(v)) for k, v in sorted(per_day.items())]
    nums = [v for _, v in values]
    out: dict[str, Any] = {
        "probe": name,
        "samples": len(nums),
        "min": round(min(nums), 3),
        "mean": round(mean(nums), 3),
        "max": round(max(nums), 3),
        "daily_means": [
            {"date": (datetime(1970, 1, 1) + timedelta(days=k)).strftime("%Y-%m-%d"),
             "mean": round(v, 3), "samples": len(per_day[k])}
            for k, v in daily
        ],
    }
    if len(daily) >= 3:
        # Drop the first and last days: they are usually partial windows.
        whole = daily[1:-1]
        if len(whole) >= 2:
            out["drift_per_day"] = round((whole[-1][1] - whole[0][1]) / (len(whole) - 1), 4)
            out["drift_method"] = "day-over-day mean, partial end days excluded"
    if "drift_per_day" not in out:
        out["drift_note"] = "needs at least 4 days of history for a drift figure"
    return out


async def outlet_events(
    client: ApexLike, outlet: str | None = None, days: int = 1, limit: int = 200
) -> dict[str, Any]:
    """Exact outlet on/off times from /rest/olog. Only outlets with logging enabled appear."""
    days = max(1, min(days, 30))
    start = datetime.now() - timedelta(days=days - 1)  # olog sdate is the START date
    events = _olog_events(await client.get_olog(days, _yymmdd(start)))
    if outlet:
        ref = outlet.lower()
        events = [e for e in events
                  if str(e.get("did", "")).lower() == ref or str(e.get("name", "")).lower() == ref]
    events.sort(key=lambda e: e["date"])
    shown = events[-limit:]
    return {
        "count": len(events),
        "events": [{"t": _iso(e["date"]), "did": e.get("did"), "name": e.get("name"),
                    "state": e.get("value")} for e in shown],
        "note": None if events else "no events. The outlet may not have logging enabled.",
    }


def _olog_events(resp: Any) -> list[dict[str, Any]]:
    out = []
    for rec in _find_records(resp):
        ts = _float(rec.get("date"))
        if ts is not None and rec.get("did") is not None and rec.get("value") is not None:
            out.append({**rec, "date": ts})
    return out


def compute_duty(
    events: list[dict[str, Any]], start: float, end: float, currently_on: bool
) -> dict[str, Any]:
    """On-time inside [start, end] from toggle events. Pure, for testing."""
    evs = sorted((e for e in events if start <= e["date"] <= end), key=lambda e: e["date"])
    if not evs:
        return {"duty_pct": 100.0 if currently_on else 0.0, "on_seconds": (end - start) if currently_on else 0.0,
                "runs": 0, "mean_run_seconds": None, "note": "no events in window; assumed unchanged"}
    on = str(evs[0]["value"]).upper() not in _ON_STATES  # the state before the first toggle
    cursor, on_seconds, runs, run_lengths, run_start = start, 0.0, 0, [], start if on else None
    for e in evs:
        new_on = str(e["value"]).upper() in _ON_STATES
        if on:
            on_seconds += e["date"] - cursor
        if new_on and not on:
            runs, run_start = runs + 1, e["date"]
        elif on and not new_on and run_start is not None:
            run_lengths.append(e["date"] - run_start)
        on, cursor = new_on, e["date"]
    if on:
        on_seconds += end - cursor
    span = end - start
    return {
        "duty_pct": round(on_seconds / span * 100, 2) if span > 0 else 0.0,
        "on_seconds": round(on_seconds),
        "runs": runs,
        "mean_run_seconds": round(mean(run_lengths), 1) if run_lengths else None,
    }


async def duty_cycle(client: ApexLike, outlet: str, days: int = 1) -> dict[str, Any]:
    """Share of time an outlet was on, run count and mean run length, from the outlet log."""
    out = await _find_outlet(client, outlet)
    days = max(1, min(days, 30))
    start_day = datetime.now() - timedelta(days=days - 1)  # olog sdate is the START date
    raw_events = _olog_events(await client.get_olog(days, _yymmdd(start_day)))
    mine = [e for e in raw_events if str(e.get("did", "")).lower() == out.did.lower()]
    end = time.time()
    result = compute_duty(mine, end - days * 86400, end, out.energized)
    return {"outlet": out.name, "did": out.did, "days": days,
            "events_in_window": len(mine), **result}


async def outlet_program(client: ApexLike, outlet: str | None = None) -> Any:
    cfg = await client.get_config()
    items = [o for o in cfg.get("oconf") or [] if isinstance(o, dict)]
    if outlet is None:
        return [{"did": o.get("did"), "name": o.get("name"), "type": o.get("type"),
                 "prog": o.get("prog")} for o in items]
    ref = outlet.lower()
    for o in items:
        if str(o.get("did", "")).lower() == ref or str(o.get("name", "")).lower() == ref:
            return {"did": o.get("did"), "name": o.get("name"), "type": o.get("type"),
                    "prog": o.get("prog")}
    raise ToolError(f"no outlet matching {outlet!r}")


# -- write tools ------------------------------------------------------------


async def set_outlet(client: ApexLike, settings: Settings, outlet: str, mode: str) -> dict[str, Any]:
    _require_write(settings)
    mode = mode.strip().upper()
    if mode not in ("AUTO", "ON", "OFF"):
        raise ToolError("mode must be AUTO, ON or OFF")
    target = await _find_outlet(client, outlet, fresh=True)
    if mode != "AUTO" and settings.is_protected(target.did, target.name):
        raise ToolError(
            f"{target.name} is protected (APEX_PROTECTED_OUTLETS). "
            "Only AUTO is allowed, which hands control back to its program."
        )
    before = target.to_dict()
    await client.set_outlet(target.did, mode)
    await asyncio.sleep(POST_WRITE_DELAY)
    after = await _find_outlet(client, target.did, fresh=True)
    return {"requested": mode, "applied": after.mode == mode, "before": before,
            "after": after.to_dict(),
            "reminder": None if mode == "AUTO" else
            "Manual ON/OFF overrides the outlet's program until you set it back to AUTO."}


async def start_feed(client: ApexLike, settings: Settings, cycle: str) -> dict[str, Any]:
    _require_write(settings)
    key = cycle.strip().upper()
    if key not in _FEED:
        raise ToolError("cycle must be A, B, C or D")
    await client.start_feed(_FEED[key])
    await asyncio.sleep(POST_WRITE_DELAY)
    return {"requested": key, "feed": (await _snapshot(client, fresh=True)).feed.to_dict()}


async def cancel_feed(client: ApexLike, settings: Settings) -> dict[str, Any]:
    """Cancel only when a real feed is running.

    Cancelling while idle corrupts the controller's feed block for about 17 hours.
    """
    _require_write(settings)
    snap = await _snapshot(client, fresh=True)
    if snap.feed.cycle is None:
        return {"cancelled": False,
                "reason": "no feed cycle is running; cancel ignored (it would corrupt the feed block)"}
    await client.cancel_feed()
    await asyncio.sleep(POST_WRITE_DELAY)
    return {"cancelled": True, "feed": (await _snapshot(client, fresh=True)).feed.to_dict()}


def validate_program(prog: str) -> list[str]:
    problems = []
    for n, line in enumerate(prog.split("\n"), 1):
        text = line.strip()
        if text and text.split(None, 1)[0].lower() not in _DIRECTIVES:
            problems.append(f"line {n}: unknown directive {text.split(None, 1)[0]!r}")
    return problems


async def update_program(
    client: ApexLike, settings: Settings, outlet: str, new_prog: str, confirm: bool = False
) -> dict[str, Any]:
    """Preview, then (with confirm=true) replace an outlet's program. Backs up the old one first."""
    _require_write(settings)
    if not settings.allow_program_write:
        raise ToolError("program edits are disabled. Set APEX_ALLOW_PROGRAM_WRITE=true to enable them.")
    cfg = await client.get_config()
    obj = next((o for o in cfg.get("oconf") or [] if isinstance(o, dict) and (
        str(o.get("did", "")).lower() == outlet.lower()
        or str(o.get("name", "")).lower() == outlet.lower())), None)
    if obj is None:
        raise ToolError(f"no outlet matching {outlet!r}")
    did, name = str(obj["did"]), str(obj.get("name", obj["did"]))
    if settings.is_protected(did, name):
        raise ToolError(f"{name} is protected (APEX_PROTECTED_OUTLETS); its program cannot be edited here.")

    prog = new_prog.replace("\r\n", "\n").rstrip("\n") + "\n"
    problems = validate_program(prog)
    if problems:
        raise ToolError("program rejected: " + "; ".join(problems))
    old = str(obj.get("prog", ""))
    diff = "".join(difflib.unified_diff(old.splitlines(True), prog.splitlines(True),
                                        "current", "proposed"))
    if old == prog:
        return {"changed": False, "reason": "proposed program is identical to the current one"}
    if not confirm:
        return {"applied": False, "outlet": name, "did": did, "diff": diff,
                "next": "Call again with confirm=true to apply. The old program is backed up first."}

    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    safe_did = re.sub(r"[^A-Za-z0-9_-]", "_", did)  # the id comes from the controller
    backup = Path(settings.backup_dir) / f"{safe_did}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    backup.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    await client.put_outlet_config({**obj, "prog": prog})
    verify = await client.get_config()
    saved = next((o for o in verify.get("oconf") or [] if str(o.get("did")) == did), {})
    return {"applied": saved.get("prog") == prog, "outlet": name, "did": did,
            "backup": str(backup), "diff": diff}
