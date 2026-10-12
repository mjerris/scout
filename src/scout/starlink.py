"""Starlink: the dish's own local API (gRPC at 192.168.100.1:9200), no account and no
sign-in. Off unless `[starlink] enabled = true`.

    status()        -> facts now: online, latency, drops, obstruction, alerts, outages
    report(...)     -> compact text with exact values (for Claude)
    speak(...)      -> one or two spoken sentences (tier 0)
    Watch           -> what to announce as status changes (outage, back online, alerts)
    Usage           -> data used per day, tallied from the dish's per-second history

The message definitions come from the dish itself (gRPC reflection), so firmware
updates that move fields don't break this.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .config import DATA, load
from .mac import ToolError

log = logging.getLogger(__name__)

USAGE = DATA / "state" / "starlink_usage.json"


class Dish:
    """A connection to the dish. Blocking gRPC calls run in a worker thread."""

    def __init__(self, address: str) -> None:
        self.address = address
        self._lock = threading.Lock()
        self._call: Any = None
        self._req: Any = None

    def _connect(self) -> None:
        # Scout's subprocesses (osascript, the Claude CLI) exec right after fork, so gRPC's
        # fork handlers aren't needed; with them on, every fork logs warnings.
        os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "false")
        import grpc
        from google.protobuf import descriptor_pb2, descriptor_pool
        from google.protobuf.message_factory import GetMessageClass
        from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc

        ch = grpc.insecure_channel(self.address)
        stub = reflection_pb2_grpc.ServerReflectionStub(ch)
        ask = reflection_pb2.ServerReflectionRequest(file_containing_symbol="SpaceX.API.Device.Device")
        resp = next(stub.ServerReflectionInfo(iter([ask]), timeout=5))
        files = {}
        for raw in resp.file_descriptor_response.file_descriptor_proto:
            fd = descriptor_pb2.FileDescriptorProto.FromString(raw)
            files[fd.name] = fd
        pool = descriptor_pool.DescriptorPool()
        added: set[str] = set()

        def add(fd: Any) -> None:  # dependencies first
            for dep in fd.dependency:
                if dep in files and dep not in added:
                    add(files[dep])
            if fd.name not in added:
                pool.Add(fd)
                added.add(fd.name)

        for fd in files.values():
            add(fd)
        req = GetMessageClass(pool.FindMessageTypeByName("SpaceX.API.Device.Request"))
        res = GetMessageClass(pool.FindMessageTypeByName("SpaceX.API.Device.Response"))
        self._req = req
        self._call = ch.unary_unary(
            "/SpaceX.API.Device.Device/Handle",
            request_serializer=req.SerializeToString,
            response_deserializer=res.FromString,
        )

    def get(self, name: str) -> dict[str, Any]:
        """One request ("get_status", "get_history"): the response as a dict."""
        from google.protobuf import json_format

        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._call is None:
                        self._connect()
                    req = self._req()
                    getattr(req, name).SetInParent()
                    out = json_format.MessageToDict(
                        self._call(req, timeout=5), preserving_proto_field_name=True
                    )
                    return dict(out)
                except Exception as exc:
                    self._call = None  # reconnect next time (the dish rebooted, the cable moved)
                    if attempt == 2:
                        raise ToolError(f"can't reach the Starlink dish at {self.address}: {exc}") from None
        raise AssertionError("unreachable")

    async def aget(self, name: str) -> dict[str, Any]:
        return await asyncio.get_running_loop().run_in_executor(None, self.get, name)


_dish: Dish | None = None


def dish() -> Dish:
    global _dish
    cfg = load().starlink
    if not cfg.enabled:
        raise ToolError("Starlink is turned off in Scout's config ([starlink] enabled = true turns it on).")
    if _dish is None or _dish.address != cfg.address:
        _dish = Dish(cfg.address)
    return _dish


# --- facts ---------------------------------------------------------------------------


def _words(enum: str) -> str:
    """'EVENT_REASON_OUTAGE_NO_DOWNLINK' / 'NO_SCHEDULE' -> 'no downlink' / 'no schedule'."""
    s = enum.rsplit("OUTAGE_", 1)[-1].lower().replace("_", " ")
    return {"no sats": "no satellites in view", "obstructed": "obstructed", "booting": "booting up"}.get(s, s)


def _outages(hist: dict[str, Any] | None, since_s: float, now: float) -> list[dict[str, Any]]:
    """Outages in the dish's history that ended after `now - since_s`."""
    out = []
    for o in (hist or {}).get("outages") or []:
        start = int(o.get("start_timestamp_ns", 0)) / 1e9
        dur = int(o.get("duration_ns", 0)) / 1e9
        if start + dur >= now - since_s:
            out.append({"start": start, "seconds": dur, "cause": _words(str(o.get("cause", "unknown")))})
    return out


def facts(
    status: dict[str, Any], hist: dict[str, Any] | None = None, now: float | None = None
) -> dict[str, Any]:
    """The parts of get_status (and get_history) that matter, in plain units."""
    now = time.time() if now is None else now
    s = status.get("dish_get_status") or {}
    obs = s.get("obstruction_stats") or {}
    outage = s.get("outage")
    f: dict[str, Any] = {
        "online": not outage and "pop_ping_latency_ms" in s,
        "outage": _words(str(outage.get("cause", "unknown"))) if outage else None,
        "outage_s": int(outage.get("duration_ns", 0)) / 1e9 if outage else 0.0,
        "latency_ms": s.get("pop_ping_latency_ms"),
        "drop": float(s.get("pop_ping_drop_rate") or 0.0),
        "down_mbps": float(s.get("downlink_throughput_bps") or 0.0) / 1e6,
        "up_mbps": float(s.get("uplink_throughput_bps") or 0.0) / 1e6,
        "obstructed": float(obs.get("fraction_obstructed") or 0.0),
        "obstructed_now": bool(obs.get("currently_obstructed")),
        "alerts": sorted(k for k, v in (s.get("alerts") or {}).items() if v is True),
        "power_w": (s.get("upsu_stats") or {}).get("dish_power"),
        "uptime_s": int((s.get("device_state") or {}).get("uptime_s") or 0),
        "update": s.get("software_update_state", ""),
        "limited": [
            r for r in (s.get("dl_bandwidth_restricted_reason"), s.get("ul_bandwidth_restricted_reason"))
            if r and r != "NO_LIMIT"
        ],
    }  # fmt: skip
    if hist:
        h = hist.get("dish_get_history") or hist
        drops = [float(x) for x in h.get("pop_ping_drop_rate") or []]
        lat = [float(x) for x in h.get("pop_ping_latency_ms") or [] if x]
        f["drop_15m"] = statistics.fmean(drops) if drops else 0.0
        f["latency_15m"] = statistics.median(lat) if lat else None
        f["outages_hour"] = _outages(h, 3600, now)
    return f


async def status() -> dict[str, Any]:
    d = dish()
    st, hist = await asyncio.gather(d.aget("get_status"), d.aget("get_history"))
    return facts(st, hist)


def _ago(seconds: float) -> str:
    if seconds < 90:
        return f"{round(seconds)} seconds"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes"
    return f"{seconds / 3600:.1f} hours"


def speak(f: dict[str, Any], ask: str = "status", usage: dict[str, float] | None = None) -> str:
    """Spoken answer. ask: status, slow, usage."""
    if ask == "usage":
        if not usage:
            return (
                "I don't have Starlink usage yet; I start counting once Starlink is turned on in my config."
            )
        out = f"Starlink today: {_gb(usage['today'])}."
        if usage.get("month"):
            out += f" This month so far: {_gb(usage['month'])}."
        return out
    if not f["online"]:
        return f"Starlink is down: {f['outage'] or 'not connected'}, for {_ago(f['outage_s'])} so far."
    if ask == "slow":
        why = _why_slow(f)
        if not why:
            return (
                f"Starlink looks healthy: {round(f['latency_ms'])} millisecond latency and no dropped pings. "
                "The slowdown is more likely the Wi-Fi or the site itself."
            )
        return "Starlink: " + "; ".join(why) + "."
    out = f"Starlink is online: {round(f['latency_ms'])} millisecond latency"
    out += f", moving {f['down_mbps']:.0f} megabits down right now" if f["down_mbps"] >= 1 else ""
    out += "."
    hour = f.get("outages_hour") or []
    if hour:
        longest = max(o["seconds"] for o in hour)
        out += f" {len(hour)} short {'dropout' if len(hour) == 1 else 'dropouts'} in the last hour, the longest {_ago(longest)}."
    if f["alerts"]:
        out += " Alerts: " + ", ".join(a.replace("_", " ") for a in f["alerts"]) + "."
    return out


def _why_slow(f: dict[str, Any]) -> list[str]:
    why = []
    if f.get("drop_15m", f["drop"]) >= 0.02:
        why.append(
            f"{f.get('drop_15m', f['drop']) * 100:.0f} percent of pings dropped in the last 15 minutes"
        )
    hour = f.get("outages_hour") or []
    if len(hour) >= 5:
        causes = sorted({o["cause"] for o in hour})
        why.append(f"{len(hour)} dropouts in the last hour ({', '.join(causes)})")
    if (f.get("latency_15m") or f["latency_ms"] or 0) >= 80:
        why.append(f"latency is high at {round(f.get('latency_15m') or f['latency_ms'])} milliseconds")
    if f["obstructed"] >= 0.05 or f["obstructed_now"]:
        why.append(
            f"the view is {f['obstructed'] * 100:.0f} percent obstructed"
            + (", blocked right now" if f["obstructed_now"] else "")
        )
    if f["down_mbps"] >= 50:
        why.append(f"something is already pulling {f['down_mbps']:.0f} megabits")
    if f["limited"]:
        why.append(
            "bandwidth is limited by the plan ("
            + ", ".join(r.lower().replace("_", " ") for r in f["limited"])
            + ")"
        )
    if any("thermal" in a for a in f["alerts"]):
        why.append("it's slowing down to cool off")
    return why  # fmt: skip


def report(f: dict[str, Any], usage: dict[str, float] | None = None) -> str:
    """Compact text with exact values, for Claude."""
    lines = [
        "Starlink (from the dish's local API):",
        f"online {f['online']}"
        + (f", outage: {f['outage']} for {f['outage_s']:.0f} s" if not f["online"] else ""),
        f"latency {f['latency_ms']} ms (15-min median {f.get('latency_15m')}), ping drop now {f['drop']:.3f}, "
        f"15-min {f.get('drop_15m', 0):.3f}",
        f"throughput now: {f['down_mbps']:.1f} Mbps down, {f['up_mbps']:.2f} Mbps up (current traffic, not capacity)",
        f"obstructed {f['obstructed'] * 100:.2f}% of the sky view, blocked now {f['obstructed_now']}",
        f"alerts: {', '.join(f['alerts']) or 'none'}; bandwidth limits: {', '.join(f['limited']) or 'none'}",
        f"dish power {f['power_w']} W, up {_ago(f['uptime_s'])}, software update state {f['update']}",
    ]
    hour = f.get("outages_hour") or []
    lines.append(
        f"dropouts in the last hour: {len(hour)}"
        + (": " + "; ".join(f"{o['cause']} {o['seconds']:.1f} s" for o in hour[-10:]) if hour else "")
    )
    if usage:
        lines.append(f"data used today {_gb(usage['today'])}, this month {_gb(usage.get('month', 0))}")
    return "\n".join(lines)


async def report_now() -> str:
    return report(await status(), Usage().summary())


def _gb(n: float) -> str:
    return f"{n / 1e9:.1f} gigabytes" if n >= 1e8 else f"{n / 1e6:.0f} megabytes"


# --- watching -------------------------------------------------------------------------


@dataclass
class Watch:
    """What to announce as the status changes. Feed it facts (or None: unreachable)."""

    outage_after_s: float = 60.0
    ignore: tuple[str, ...] = ("is_heating",)
    _down_since: float | None = None
    _announced_down: bool = False
    _alerts: set[str] = field(default_factory=set)

    def update(self, f: dict[str, Any] | None, now: float) -> list[str]:
        said: list[str] = []
        online = f is not None and f["online"]
        if not online:
            if self._down_since is None:
                self._down_since = now - (f["outage_s"] if f else 0.0)
            if not self._announced_down and now - self._down_since >= self.outage_after_s:
                self._announced_down = True
                said.append(
                    f"Starlink is down: {f['outage'] or 'not connected'}."
                    if f
                    else "I can't reach the Starlink dish."
                )
        else:
            if self._announced_down and self._down_since is not None:
                said.append(f"Starlink's back, after {_ago(now - self._down_since)}.")
            self._down_since, self._announced_down = None, False
        if f is not None:
            now_alerts = {a for a in f["alerts"] if a not in self.ignore}
            said.extend(f"Starlink alert: {a.replace('_', ' ')}." for a in sorted(now_alerts - self._alerts))
            self._alerts = now_alerts
        return said


def in_quiet_hours(spec: str, now: dt.datetime) -> bool:
    """'22:00-08:00' (may wrap midnight); empty = never quiet."""
    if not spec or "-" not in spec:
        return False
    a, b = (dt.time.fromisoformat(x.strip()) for x in spec.split("-", 1))
    t = now.time()
    return a <= t < b if a <= b else (t >= a or t < b)


class Usage:
    """Bytes per local day, tallied from the dish's per-second history ring (900 s long,
    so this must be polled at least every 15 minutes)."""

    def __init__(self, path: Any = None) -> None:
        self.path = path or USAGE
        self._last: int | None = None

    def _load(self) -> dict[str, Any]:
        try:
            data: dict[str, Any] = json.loads(self.path.read_text())
            return data
        except (OSError, ValueError):
            return {}

    def add(self, hist: dict[str, Any], today: dt.date) -> float:
        """Count the samples since the last call; returns the bytes added."""
        h = hist.get("dish_get_history") or hist
        current = int(h.get("current") or 0)
        down = [float(x) for x in h.get("downlink_throughput_bps") or []]
        up = [float(x) for x in h.get("uplink_throughput_bps") or []]
        n = len(down)
        if not n or not current:
            return 0.0
        last = self._last if self._last is not None and self._last <= current else current - min(n, 60)
        new = min(current - last, n)
        self._last = current
        total = 0.0
        for c in range(current - new, current):  # sample for counter c lives at c % n
            total += (down[c % n] + (up[c % len(up)] if up else 0.0)) / 8
        data = self._load()
        key = today.isoformat()
        data[key] = data.get(key, 0.0) + total
        for k in sorted(data)[:-62]:  # about two months
            del data[k]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=1))
        except OSError:
            pass
        return total

    def summary(self, today: dt.date | None = None) -> dict[str, float]:
        today = today or dt.date.today()
        data = self._load()
        if not data:
            return {}
        month = sum(v for k, v in data.items() if k.startswith(today.isoformat()[:7]))
        return {"today": float(data.get(today.isoformat(), 0.0)), "month": float(month)}


async def watch(announce: Any) -> None:
    """Poll the dish forever: announce outages and alerts, tally usage. `announce(text)`."""
    cfg = load().starlink
    w = Watch(outage_after_s=cfg.announce_outage_s, ignore=tuple(cfg.ignore_alerts))
    usage = Usage()
    last_hist = 0.0
    while True:
        f: dict[str, Any] | None
        try:
            d = dish()
            f = facts(await d.aget("get_status"))
            if time.monotonic() - last_hist >= 300:
                usage.add(await d.aget("get_history"), dt.date.today())
                last_hist = time.monotonic()
        except ToolError as exc:
            log.info("starlink: %s", exc)
            f = None
        except Exception:
            log.exception("starlink watch")
            f = None
        try:  # a bad quiet_hours or a failed announce must not end the watch
            for text in w.update(f, time.time()):
                if cfg.announce and not in_quiet_hours(cfg.quiet_hours, dt.datetime.now()):
                    await announce(text)
                else:
                    log.info("starlink (not announced): %s", text)
        except Exception:
            log.exception("starlink announce")
        await asyncio.sleep(cfg.poll_s)
