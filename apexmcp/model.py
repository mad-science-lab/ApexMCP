"""Turn a raw /rest/status payload into a small typed snapshot.

Firmware quirks live here (verified on AOS 5.15L, see the ReefHub project):

* Outlet state is `status[0]` ("AON"/"AOF"/"TBL"/"ON"/"OFF"), not a scalar.
* `feed` is {"name": 0-4, "active": <seconds remaining>}. Cancelling while idle
  makes the controller report garbage (name=256, active underflowed) for ~17 h,
  so both fields are range-checked before anyone sees them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

FEED_LETTERS = {1: "A", 2: "B", 3: "C", 4: "D"}
_MAX_FEED_SECONDS = 3600


@dataclass(frozen=True)
class Outlet:
    did: str
    name: str
    type: str
    mode: str          # AUTO | ON | OFF
    energized: bool
    raw_state: str = ""            # exactly what the Apex says: AON / AOF / ON / OFF / TBL
    percent: int | None = None     # power level, only for outlets that report one
    pump_mode: str | None = None   # MXM pumps: status[2], e.g. "Off", "Cnst"

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class Probe:
    did: str
    name: str
    type: str          # Temp | pH | Amps | pwr | volts | digital | Cond | ...
    value: float | bool | None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"did": self.did, "name": self.name, "type": self.type,
                               "value": self.value}
        if self.type == "Temp" and isinstance(self.value, float):
            # The Apex reports no unit with the reading; this is a heuristic.
            out["unit_guess"] = "C" if self.value <= 45 else "F"
        return out


@dataclass(frozen=True)
class Feed:
    cycle: int | None          # 1..4, None when idle
    seconds_remaining: int     # 0 when idle
    raw_name: int | None       # unvalidated feed.name, used to guard cancel

    @property
    def mode(self) -> str:
        return FEED_LETTERS.get(self.cycle, "OFF") if self.cycle else "OFF"

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "seconds_remaining": self.seconds_remaining}


@dataclass(frozen=True)
class Snapshot:
    serial: str
    hostname: str
    software: str
    hardware: str
    system_type: str = ""
    modules: dict[int, str] = field(default_factory=dict)   # abaddr -> hwtype
    outlets: list[Outlet] = field(default_factory=list)
    probes: list[Probe] = field(default_factory=list)
    feed: Feed = Feed(None, 0, None)


def _float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _percent(status: Any) -> int | None:
    """Variable outputs report ["AON", "90", "OK", ""]; plain outlets have no number."""
    if not isinstance(status, list):
        return None
    for item in status:
        text = str(item).strip().rstrip("%") if item is not None else ""
        if text.isdigit() and 0 <= int(text) <= 100:
            return int(text)
    return None


def _pump_mode(full_type: str, status: Any) -> str | None:
    """MXM pumps carry their running mode as text in the third status slot."""
    if not full_type.startswith("MXMPump") or not isinstance(status, list) or len(status) < 3:
        return None
    text = str(status[2] or "").strip()
    return text or None


def _outlet(o: dict[str, Any]) -> Outlet | None:
    did = str(o.get("did", ""))
    if not did:
        return None
    status = o.get("status")
    raw = str(o.get("state", "")).strip().upper()
    if not raw and isinstance(status, list) and status:
        raw = str(status[0] or "").strip().upper()
    mode = raw if raw in ("ON", "OFF") else "AUTO"  # AON / AOF / TBL: under program control
    return Outlet(
        did=did,
        name=str(o.get("name") or did),
        type=str(o.get("type", "")),
        mode=mode,
        energized=raw in ("AON", "ON", "TBL"),
        raw_state=raw,
        percent=_percent(status),
        pump_mode=_pump_mode(str(o.get("type", "")), status),
    )


def _probes(inputs: Any) -> list[Probe]:
    if isinstance(inputs, dict):
        items = [(str(k), v) for k, v in inputs.items() if isinstance(v, dict)]
    elif isinstance(inputs, list):
        items = [(str(v.get("did", "")), v) for v in inputs if isinstance(v, dict)]
    else:
        return []
    out: list[Probe] = []
    for did, p in items:
        if not did:
            continue
        ptype = str(p.get("type", ""))
        value: float | bool | None
        if ptype == "digital":
            n = _float(p.get("value"))
            value = None if n is None else bool(n)
        else:
            value = _float(p.get("value"))
        out.append(Probe(did=did, name=str(p.get("name") or did), type=ptype, value=value))
    return out


def _feed(feed: Any) -> Feed:
    feed = feed if isinstance(feed, dict) else {}
    name, active = feed.get("name"), feed.get("active")
    raw = name if isinstance(name, int) else None
    cycle = name if isinstance(name, int) and 1 <= name <= 4 else None
    secs = active if cycle and isinstance(active, int) and 0 < active <= _MAX_FEED_SECONDS else 0
    return Feed(cycle=cycle, seconds_remaining=secs, raw_name=raw)


def parse_status(status: dict[str, Any]) -> Snapshot:
    # AOS 5.15 nests the system block under "system"; older builds use "istat".
    system = status.get("system") or status.get("istat") or {}
    outlets = [o for o in (_outlet(x) for x in status.get("outputs") or []
                           if isinstance(x, dict)) if o]
    return Snapshot(
        serial=str(system.get("serial", "")) or "apex",
        hostname=str(system.get("hostname", "")),
        software=str(system.get("software", "")),
        hardware=str(system.get("hardware", "")),
        system_type=str(system.get("type", "")),
        modules={
            m["abaddr"]: str(m.get("hwtype", "")).upper()
            for m in status.get("modules") or []
            if isinstance(m, dict) and isinstance(m.get("abaddr"), int)
        },
        outlets=outlets,
        probes=_probes(status.get("inputs") or {}),
        feed=_feed(status.get("feed")),
    )
