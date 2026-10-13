"""Fewer process spawns and a faster first Granola sync: the remembered Reminders access
check, Tailscale's address read off the interfaces, and full notes fetched a few at a
time under the same request rate. Fakes only: no network, no osascript, no real vcal."""

from __future__ import annotations

import asyncio
import io
import itertools
import json
import stat
import threading
import time
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from scout import calendar_mac, granola, index, reminders_mac
from scout import web as web_mod
from scout.mac import ToolError

# --- Reminders access, remembered ---------------------------------------------------


def _vcal(tmp_path: Path, status: str = "full_access", refuse: bool = False) -> Path:
    """A fake helper logging each call; `refuse` answers operations as the real helper
    does when access was taken away."""
    log = tmp_path / "calls.jsonl"
    exe = tmp_path / "vcal"
    exe.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "cmd = sys.argv[1]\n"
        f"if cmd == 'status': print(json.dumps({{'status': {status!r}}}))\n"
        f"elif {refuse!r}:\n"
        "    print(json.dumps({'error': 'Reminders access is denied: allow it in System Settings'}))\n"
        "    sys.exit(1)\n"
        "else: print(json.dumps({'reminders': [], 'total': 0, 'lists': []}))\n"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def _statuses(tmp_path: Path) -> int:
    lines = (tmp_path / "calls.jsonl").read_text().splitlines()
    return sum(json.loads(line)[0] == "status" for line in lines)


@pytest.fixture(autouse=True)
def _fresh_access() -> None:
    reminders_mac._access.clear()


def test_granted_reminders_access_is_asked_once_for_a_while(tmp_path: Path) -> None:
    exe = _vcal(tmp_path)

    async def go() -> None:
        await reminders_mac.reminder_data(helper=exe)
        await reminders_mac.lists(helper=exe)
        await reminders_mac.list_names(helper=exe)

    asyncio.run(go())
    assert _statuses(tmp_path) == 1
    until, refusal = reminders_mac._access[str(exe)]
    assert refusal == "" and until - time.monotonic() > reminders_mac._GRANTED_S - 5
    reminders_mac._access[str(exe)] = (time.monotonic() - 1, "")  # five minutes on
    asyncio.run(reminders_mac.lists(helper=exe))
    assert _statuses(tmp_path) == 2


def test_a_refusal_is_remembered_briefly(tmp_path: Path) -> None:
    exe = _vcal(tmp_path, status="denied")
    for _ in range(3):
        with pytest.raises(ToolError, match="Reminders access is denied"):
            asyncio.run(reminders_mac.lists(helper=exe))
    assert _statuses(tmp_path) == 1
    until, refusal = reminders_mac._access[str(exe)]
    assert refusal and until - time.monotonic() <= reminders_mac._DENIED_S
    reminders_mac._access[str(exe)] = (time.monotonic() - 1, refusal)  # a minute on
    exe = _vcal(tmp_path)  # allowed in System Settings meanwhile
    assert asyncio.run(reminders_mac.lists(helper=exe)) == "No reminder lists on this Mac."
    assert _statuses(tmp_path) == 2


def test_an_access_error_from_an_operation_forgets_the_grant(tmp_path: Path) -> None:
    exe = _vcal(tmp_path)
    asyncio.run(reminders_mac.lists(helper=exe))
    exe = _vcal(tmp_path, status="denied", refuse=True)  # taken away since
    with pytest.raises(ToolError, match="access is denied"):
        asyncio.run(reminders_mac.lists(helper=exe))
    assert str(exe) not in reminders_mac._access
    with pytest.raises(ToolError, match=r"Reminders access is denied\. Turn it on"):
        asyncio.run(reminders_mac.lists(helper=exe))  # asked the helper again
    assert _statuses(tmp_path) == 2


def test_other_errors_keep_the_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _vcal(tmp_path)
    asyncio.run(reminders_mac.lists(helper=exe))

    async def no_list(*args: str, helper: Path | None = None) -> dict[str, Any]:
        raise ToolError("no reminder list named Groceries")

    monkeypatch.setattr(calendar_mac, "vcal", no_list)
    with pytest.raises(ToolError, match="Groceries"):
        asyncio.run(reminders_mac.lists(helper=exe))
    assert reminders_mac._access[str(exe)][1] == ""


# --- Tailscale's address, without spawning tailscale ---------------------------------


def _no_command(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    ran: list[str] = []

    def exes() -> list[str]:
        ran.append("tailscale")
        return []

    monkeypatch.setattr(web_mod, "_tailscale_exes", exes)
    return ran


def test_tailscale_address_comes_from_the_tunnel_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _no_command(monkeypatch)
    monkeypatch.setattr(
        web_mod,
        "_interface_ipv4s",
        lambda: [("lo0", "127.0.0.1"), ("en0", "100.70.1.2"), ("utun4", "100.101.102.103")],
    )
    assert asyncio.run(web_mod._tailscale_ip()) == "100.101.102.103"
    assert ran == []  # the command wasn't needed


def test_tailscale_falls_back_to_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _no_command(monkeypatch)
    for found in (
        [("lo0", "127.0.0.1"), ("utun2", "10.8.0.2")],  # no Tailscale address
        [("utun3", "100.64.0.5"), ("utun5", "100.90.0.7")],  # two: which is Tailscale's?
    ):
        monkeypatch.setattr(web_mod, "_interface_ipv4s", lambda found=found: found)
        assert asyncio.run(web_mod._tailscale_ip()) is None
    assert ran == ["tailscale", "tailscale"]


def test_the_interfaces_are_read_without_a_process() -> None:
    found = web_mod._interface_ipv4s()
    assert any(ip == "127.0.0.1" for _, ip in found)
    assert all(name and ip.count(".") == 3 for name, ip in found)


# --- Granola: full notes a few at a time -----------------------------------------------


class SlowGranola:
    """Lists `n` notes; each full note takes `delay` to answer. Records how many were
    being answered at once."""

    def __init__(self, n: int, delay: float = 0.05, fail: str | None = None) -> None:
        self.ids = [f"not_{i:014d}" for i in range(n)]
        self.delay, self.fail = delay, fail
        self.lock = threading.Lock()
        self.busy = self.most = 0

    def __call__(self, path: str, query: dict[str, Any]) -> dict[str, Any]:
        if path == "/v1/notes":
            return {
                "notes": [
                    {"id": nid, "updated_at": f"2026-10-01T10:00:{i:02d}Z", "deleted_at": None}
                    for i, nid in enumerate(self.ids)
                ],
                "hasMore": False,
            }
        nid = path.split("/")[3]
        with self.lock:
            self.busy += 1
            self.most = max(self.most, self.busy)
        try:
            time.sleep(self.delay * (3 if nid == self.ids[0] else 1))  # the first is slowest
            if nid == self.fail:
                raise granola.GranolaError(f"{path}: HTTP 500")
            return {"id": nid, "title": f"Meeting {nid}", "updated_at": "2026-10-01T10:00:00Z",
                    "summary_text": "notes", "transcript": []}  # fmt: skip
        finally:
            with self.lock:
                self.busy -= 1


class RecordingStore:
    """The index, noting the order notes are stored in."""

    def __init__(self, ix: index.Index) -> None:
        self.ix = ix
        self.order: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.ix, name)

    def put(self, **item: Any) -> bool:
        if "#" not in item["id"]:
            self.order.append(item["id"].removeprefix("granola:"))
        return bool(self.ix.put(**item))


@pytest.fixture
def gix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> index.Index:
    monkeypatch.setattr(granola, "PENDING", tmp_path / "pending.json")
    return index.Index(tmp_path / "m.db", None)


def test_full_notes_are_fetched_three_at_a_time_and_stored_in_order(gix: index.Index) -> None:
    api = SlowGranola(9)
    store = RecordingStore(gix)
    notes, _, newest = granola.sync_pass(store, api, None)  # type: ignore[arg-type]
    assert notes == 9 and newest == "2026-10-01T10:00:08Z"
    assert api.most == granola.WORKERS == 3
    assert store.order == api.ids  # listing order, though the first answered last


def test_an_error_still_stops_the_pass_and_keeps_pending(gix: index.Index, tmp_path: Path) -> None:
    api = SlowGranola(6, fail="not_00000000000002")
    pending = tmp_path / "pending.json"
    pending.write_text(json.dumps({"not_99999999999999": 1.0}))
    with pytest.raises(granola.GranolaError, match="HTTP 500"):
        granola.sync_pass(gix, api, None)
    assert json.loads(pending.read_text()) == {"not_99999999999999": 1.0}
    assert api.busy == 0  # nothing left running


def _fake_open(starts: list[float], codes: list[int], lock: threading.Lock) -> Any:
    def open_(req: Any, timeout: float = 0) -> Any:
        with lock:
            starts.append(time.monotonic())
            code = codes.pop(0) if codes else 200
        if code == 429:
            headers = Message()
            headers["Retry-After"] = "0.3"
            raise urllib.error.HTTPError(req.full_url, 429, "slow down", headers, io.BytesIO())
        return io.BytesIO(b"{}")

    return open_


def test_the_fetcher_keeps_its_rate_across_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    starts: list[float] = []
    lock = threading.Lock()
    monkeypatch.setattr(granola._opener, "open", _fake_open(starts, [], lock))
    get = granola.fetcher("key", base="https://granola.invalid", gap_s=0.05)
    threads = [threading.Thread(target=lambda: [get("/v1/notes/x", {}) for _ in range(4)]) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(starts) == 12
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert min(gaps) >= 0.045  # never two closer than the gap


def test_a_429_holds_back_every_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    starts: list[float] = []
    lock = threading.Lock()
    monkeypatch.setattr(granola._opener, "open", _fake_open(starts, [429], lock))
    get = granola.fetcher("key", base="https://granola.invalid", gap_s=0.01)
    first = threading.Thread(target=lambda: get("/v1/notes/a", {}))  # a 429, then its retry
    first.start()
    while not starts:
        time.sleep(0.005)
    time.sleep(0.05)  # the 429 is in, its hold set (microseconds after)
    get("/v1/notes/b", {})  # asked during the wait
    first.join()
    assert len(starts) == 3
    assert all(t - starts[0] >= 0.29 for t in starts[1:])


def test_calendar_access_is_remembered_like_reminders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scout import calendar_mac

    calls: list[str] = []
    state = {"status": "full_access", "events_error": ""}

    async def fake_exec(*argv: str, **kw: object) -> object:
        calls.append(argv[1])

        class P:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                if argv[1] == "status":
                    return json.dumps({"status": state["status"]}).encode(), b""
                if state["events_error"]:
                    return json.dumps({"error": state["events_error"]}).encode(), b""
                return json.dumps({"events": [], "total": 0}).encode(), b""

        return P()

    helper = tmp_path / "vcal"
    helper.write_text("")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    for _ in range(3):
        asyncio.run(calendar_mac.event_data("2026-10-13", "2026-10-14", helper=helper))
    assert calls.count("status") == 1  # asked once, then remembered
    state["events_error"] = "calendar access not granted"
    with pytest.raises(ToolError):
        asyncio.run(calendar_mac.event_data("2026-10-13", "2026-10-14", helper=helper))
    state["events_error"] = ""
    asyncio.run(calendar_mac.event_data("2026-10-13", "2026-10-14", helper=helper))
    assert calls.count("status") == 2  # a refusal from a real read made it ask again
