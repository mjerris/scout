"""One-time pairing codes (phone_pairing.py) and `scoutctl phone pair`, which shows one as a
scout://pair link and a QR code."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from test_routing import make, run

from scout import phone_devices, phone_pairing

T0 = 1_760_000_000.0


@pytest.fixture
def codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state" / "phone_pair_codes.json"
    monkeypatch.setattr(phone_pairing, "PATH", path)
    return path


def test_a_code_is_short_typeable_and_lasts_ten_minutes(codes: Path) -> None:
    code, expires_at = phone_pairing.mint(now=T0)
    assert expires_at == T0 + phone_pairing.TTL_S == T0 + 600
    assert len(code) == 9 and code[4] == "-"
    assert all(c in phone_pairing.ALPHABET for c in code.replace("-", ""))
    assert not set("01ILO") & set(code)  # nothing that reads as another character


def test_only_a_hash_of_the_code_is_stored(codes: Path) -> None:
    code, _ = phone_pairing.mint(now=T0)
    text = codes.read_text()
    assert code not in text and code.replace("-", "") not in text
    want = hashlib.sha256(code.replace("-", "").encode()).hexdigest()
    assert list(json.loads(text)) == [want]
    assert stat.S_IMODE(codes.stat().st_mode) == 0o600


def test_a_code_works_once(codes: Path) -> None:
    code, _ = phone_pairing.mint(now=T0)
    assert phone_pairing.consume(code, now=T0 + 1) == "ok"
    assert phone_pairing.consume(code, now=T0 + 2) == "used"


def test_a_code_is_read_loosely(codes: Path) -> None:
    code, _ = phone_pairing.mint(now=T0)
    loose = " " + code.replace("-", "").lower() + " "
    assert phone_pairing.normalize(loose) == code.replace("-", "")
    assert phone_pairing.consume(loose, now=T0 + 1) == "ok"


def test_an_expired_code_is_refused(codes: Path) -> None:
    code, expires_at = phone_pairing.mint(now=T0)
    assert phone_pairing.consume(code, now=expires_at + 1) == "expired"
    assert phone_pairing.consume(code, now=expires_at + 2) == "expired"  # and stays refused


def test_wrong_and_empty_codes_are_unknown(codes: Path) -> None:
    phone_pairing.mint(now=T0)
    assert phone_pairing.consume("ABCD-EFGH", now=T0) == "unknown"
    assert phone_pairing.consume("", now=T0) == "unknown"
    assert phone_pairing.consume("x" * 500, now=T0) == "unknown"


def test_a_new_code_replaces_an_unused_one(codes: Path) -> None:
    old, _ = phone_pairing.mint(now=T0)
    new, _ = phone_pairing.mint(now=T0 + 5)
    assert phone_pairing.consume(old, now=T0 + 6) == "unknown"
    assert phone_pairing.consume(new, now=T0 + 6) == "ok"


def test_old_entries_are_dropped(codes: Path) -> None:
    code, _ = phone_pairing.mint(now=T0)
    phone_pairing.consume(code, now=T0 + 1)
    phone_pairing.mint(now=T0 + 2 * 86400)
    assert len(json.loads(codes.read_text())) == 1


def test_wrong_codes_are_rate_limited() -> None:
    lim = phone_pairing.Limiter(limit=5, window_s=60)
    for i in range(5):
        assert lim.allowed(T0 + i)
        lim.failed(T0 + i)
    assert not lim.allowed(T0 + 10)
    assert lim.allowed(T0 + 61)  # the window moved past the first failure


def test_the_link(codes: Path) -> None:
    link = phone_pairing.link("https://mac.tail1.ts.net", "ABCD-EFGH")
    u = urlsplit(link)
    assert (u.scheme, u.netloc) == ("scout", "pair")
    assert parse_qs(u.query) == {"server": ["https://mac.tail1.ts.net"], "code": ["ABCD-EFGH"]}


def test_scoutctl_phone_pair_shows_a_link_and_qr(
    codes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token = "web-token-" + "x" * 30
    token_path = tmp_path / "state" / "web_token"
    token_path.parent.mkdir(exist_ok=True)
    token_path.write_text(token + "\n")
    monkeypatch.setattr("scout.links.web_base", lambda refresh=False: "https://mac.tail1.ts.net")
    monkeypatch.setattr(phone_devices, "PATH", tmp_path / "devices.json")
    assert phone_devices.main(["pair"]) == 0
    out = capsys.readouterr().out
    [link] = [w for w in out.split() if w.startswith("scout://pair?")]
    q = parse_qs(urlsplit(link).query)
    assert q["server"] == ["https://mac.tail1.ts.net"]
    assert phone_pairing.consume(q["code"][0]) == "ok"
    assert "▀" in out or "▄" in out or "█" in out  # the terminal QR code
    assert token not in out and "secret" not in out.lower()
    assert not (tmp_path / "devices.json").exists()  # the phone pairs itself with the code


def test_scoutctl_phone_pair_warns_without_https(
    codes: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("scout.links.web_base", lambda refresh=False: "http://192.168.1.5:8765")
    assert phone_devices.main(["pair"]) == 0
    captured = capsys.readouterr()
    assert "scout://pair?" in captured.out
    assert "https" in captured.err and "tailscale" in captured.err.lower()


def test_scoutctl_phone_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert phone_devices.main(["bogus"]) == 2
    assert "phone pair | list | forget <device>" in capsys.readouterr().err


# --- approve by voice: a pairing request the room says yes to ------------------------------


class FakeAsk:
    """Stands in for the room's spoken question: answers when the test says."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []
        self.answer: asyncio.Future[bool | None] | None = None

    async def __call__(self, name: str, match: str) -> bool | None:
        self.asked.append((name, match))
        self.answer = asyncio.get_running_loop().create_future()
        return await self.answer


def _requests(ask: FakeAsk, devices: Path, **kw: Any) -> phone_pairing.PairRequests:
    return phone_pairing.PairRequests(ask, lambda name: phone_devices.pair(name, devices), **kw)


def test_a_request_the_room_approves_gets_its_secret_once(tmp_path: Path) -> None:
    devices = tmp_path / "devices.json"

    async def main() -> None:
        ask = FakeAsk()
        reqs = _requests(ask, devices)
        started = reqs.start("Mike's iPhone")
        assert set(started) == {"request", "match", "expires_at"}
        assert re.fullmatch(r"[1-9][0-9]", started["match"]) and len(started["request"]) >= 32
        await asyncio.sleep(0.01)
        assert ask.asked == [("Mike's iPhone", started["match"])]
        assert (await reqs.status(started["request"]))["status"] == "waiting"
        waiter = asyncio.create_task(reqs.status(started["request"], wait=5))  # a long poll
        await asyncio.sleep(0.01)
        assert ask.answer is not None
        ask.answer.set_result(True)
        got = await asyncio.wait_for(waiter, 2)
        assert got["status"] == "approved" and set(got) == {"status", "device", "secret"}
        assert phone_devices.load_devices(devices)[got["device"]]["secret"] == got["secret"]
        assert (await reqs.status(started["request"]))["status"] == "unknown"  # handed out once

    run(main())


@pytest.mark.parametrize(("answer", "want"), [(False, "denied"), (None, "expired")])
def test_a_request_said_no_to_or_unanswered_is_refused(
    tmp_path: Path, answer: bool | None, want: str
) -> None:
    devices = tmp_path / "devices.json"

    async def main() -> None:
        ask = FakeAsk()
        reqs = _requests(ask, devices)
        started = reqs.start("iPhone")
        await asyncio.sleep(0.01)
        assert ask.answer is not None
        ask.answer.set_result(answer)
        assert (await reqs.status(started["request"], wait=2))["status"] == want
        assert not devices.exists()

    run(main())


def test_a_request_times_out(tmp_path: Path) -> None:
    async def main() -> None:
        reqs = _requests(FakeAsk(), tmp_path / "devices.json", ttl_s=0.05)
        started = reqs.start("iPhone")
        assert (await reqs.status(started["request"], wait=2))["status"] == "expired"

    run(main())


def test_one_request_at_a_time_and_rate_limited(tmp_path: Path) -> None:
    async def main() -> None:
        ask = FakeAsk()
        reqs = _requests(ask, tmp_path / "devices.json", limit=2, window_s=600)
        reqs.start("one")
        with pytest.raises(phone_pairing.PairRefused) as busy:
            reqs.start("two")
        assert busy.value.status == 409
        await asyncio.sleep(0.01)
        assert ask.answer is not None
        ask.answer.set_result(False)
        await asyncio.sleep(0.01)
        reqs.start("three")
        await asyncio.sleep(0.01)
        ask.answer.set_result(False)
        await asyncio.sleep(0.01)
        with pytest.raises(phone_pairing.PairRefused) as limited:
            reqs.start("four")
        assert limited.value.status == 429

    run(main())


def test_unknown_requests() -> None:
    async def main() -> None:
        reqs = phone_pairing.PairRequests(FakeAsk(), lambda name: ("d", "s"))
        assert (await reqs.status("nope"))["status"] == "unknown"
        assert (await reqs.status(""))["status"] == "unknown"

    run(main())


def test_the_room_is_asked_with_the_match_number() -> None:
    async def go() -> tuple[bool | None, list[str], list[dict[str, Any]]]:
        r = make()
        events: list[dict[str, Any]] = []
        r.a.emit = lambda kind, **kw: events.append({"type": kind, **kw})  # type: ignore[method-assign]
        task = asyncio.create_task(r.a.approve_pairing("Mike's iPhone", "47"))
        await asyncio.sleep(0.05)
        r.a.answer_confirm(True, r.a._confirm_id)  # Approve on the web UI, or a spoken yes
        return await task, list(r.spk.said), events

    answer, said, events = run(go())
    assert answer is True
    assert said[0] == "Mike's iPhone wants to pair with Scout, code 47. Should I allow it?"
    [confirm] = [e for e in events if e["type"] == "confirm"]
    assert "47" in confirm["text"] and "Mike's iPhone" in confirm["text"]


# --- auto-find: Scout on the local network (Bonjour, `dns-sd -R`) --------------------------


def test_bonjour_advertises_the_https_address_and_never_a_token() -> None:
    argv = phone_pairing.bonjour_args("https://mac.tail1.ts.net", 8765, "Mike's Mac")
    assert argv == [
        "-R", "Scout on Mike's Mac", "_scout._tcp", "local", "8765",
        "url=https://mac.tail1.ts.net", "host=Mike's Mac",
    ]  # fmt: skip
    assert phone_pairing.bonjour_args("http://192.168.1.5:8765", 8765, "Mac") is None
    assert phone_pairing.bonjour_args("", 8765, "Mac") is None


def test_the_advertiser_runs_one_child_and_follows_the_address(tmp_path: Path) -> None:
    log = tmp_path / "args.log"
    stub = tmp_path / "dns-sd"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexec sleep 30\n')
    stub.chmod(0o755)

    async def started(n: int) -> list[str]:  # the stub has written its arguments
        for _ in range(200):
            lines = log.read_text().splitlines() if log.exists() else []
            if len(lines) >= n:
                return lines
            await asyncio.sleep(0.01)
        raise AssertionError(f"dns-sd stand-in started {n} times? log: {lines}")

    async def main() -> None:
        ad = phone_pairing.Advertiser(str(stub), 8765, "Mac")
        try:
            await ad.update("https://a.ts.net")
            await started(1)
            first = ad.pid
            await ad.update("https://a.ts.net")  # the same: left running
            assert ad.pid == first and first is not None
            await ad.update("https://b.ts.net")  # moved: re-advertised
            assert ad.pid not in (None, first)
            lines = await started(2)
            await ad.update("http://lan:8765")  # nothing https to offer: stopped
            assert ad.pid is None
        finally:
            await ad.close()
        assert ad.pid is None
        assert lines[0].endswith("url=https://a.ts.net host=Mac") and "url=https://b.ts.net" in lines[1]

    run(main())
