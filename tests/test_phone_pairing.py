"""One-time pairing codes (phone_pairing.py) and `scoutctl phone pair`, which shows one as a
scout://pair link and a QR code."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
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
    err = capsys.readouterr().err
    assert "phone pair | pair list | pair approve <id> | pair deny <id> | list | forget <device>" in err


# --- approval: one pending request, answered from any channel ---------------------------------


class FakeAsk:
    """Stands in for a channel that asks and waits (the room, the Mac's alert): answers
    when the test says, and notes when it was withdrawn."""

    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []
        self.answer: asyncio.Future[bool | None] | None = None
        self.cancelled = 0

    async def __call__(self, view: dict[str, Any]) -> bool | None:
        self.asked.append(view)
        self.answer = asyncio.get_running_loop().create_future()
        try:
            return await self.answer
        except asyncio.CancelledError:
            self.cancelled += 1
            raise

    def say(self, answer: bool | None) -> None:
        assert self.answer is not None
        self.answer.set_result(answer)


def _requests(devices: Path, *askers: tuple[str, FakeAsk], **kw: Any) -> phone_pairing.PairRequests:
    return phone_pairing.PairRequests(lambda name: phone_devices.pair(name, devices), askers, **kw)


WHO = {
    "login": "guest@example.com",
    "owner": False,
    "peer": "guest.tail1.ts.net",
    "ip": "100.64.0.9",
    "agent": "Scout/1.0",
}


def test_a_request_goes_to_every_channel_at_once_and_the_first_answer_wins(tmp_path: Path) -> None:
    devices = tmp_path / "devices.json"

    async def main() -> None:
        voice, mac = FakeAsk(), FakeAsk()
        reqs = _requests(devices, ("voice", voice), ("mac", mac))
        events: list[tuple[str, dict[str, Any]]] = []
        reqs.listeners.append(lambda kind, data: events.append((kind, data)))
        started = reqs.start("Mike's iPhone", WHO)
        assert set(started) == {"request", "match", "expires_at"}
        assert re.fullmatch(r"[1-9][0-9]", started["match"]) and len(started["request"]) >= 32
        await asyncio.sleep(0.01)
        [view] = reqs.pending()
        assert voice.asked == [view] and mac.asked == [view]  # both asked, with the same details
        assert view["name"] == "Mike's iPhone" and view["match"] == started["match"]
        assert {k: view[k] for k in WHO} == WHO and re.fullmatch(r"[A-Z2-9]{6}", view["id"])
        assert events == [("pair_request", view)]
        assert (await reqs.status(started["request"]))["status"] == "waiting"
        waiter = asyncio.create_task(reqs.status(started["request"], wait=5))  # the requester's long poll
        await asyncio.sleep(0.01)
        mac.say(True)  # the Mac's alert answers first
        got = await asyncio.wait_for(waiter, 2)
        await asyncio.sleep(0.01)  # the withdrawn channels wind down
        assert got["status"] == "approved" and set(got) == {"status", "device", "secret"}
        assert phone_devices.load_devices(devices)[got["device"]]["secret"] == got["secret"]
        assert voice.cancelled == 1 and mac.cancelled == 0  # the room's question was withdrawn
        assert events[-1] == (
            "pair_resolved",
            {"id": view["id"], "status": "approved", "via": "mac", "name": "Mike's iPhone"},
        )
        assert reqs.pending() == []
        # Later answers lose, from any channel; the secret went out once.
        assert await reqs.decide(view["id"], False, "cli") == {
            "status": "already",
            "result": "approved",
            "via": "mac",
        }
        assert (await reqs.status(started["request"]))["status"] == "unknown"
        assert len(phone_devices.load_devices(devices)) == 1

    run(main())


@pytest.mark.parametrize("via", ["web", "phone d0123456789abcdef", "cli"])
def test_a_request_answered_by_id_from_each_channel(tmp_path: Path, via: str) -> None:
    devices = tmp_path / "devices.json"

    async def main() -> None:
        voice = FakeAsk()
        reqs = _requests(devices, ("voice", voice))
        started = reqs.start("Guest", WHO)
        await asyncio.sleep(0.01)
        [view] = reqs.pending()
        got = await reqs.decide(view["id"], True, via)
        assert got["status"] == "approved" and "secret" not in got  # the answerer never sees the secret
        assert (await reqs.status(started["request"]))["status"] == "approved"
        await asyncio.sleep(0.01)
        assert voice.cancelled == 1

    run(main())


def test_two_answers_at_once_one_wins(tmp_path: Path) -> None:
    devices = tmp_path / "devices.json"

    async def main() -> None:
        reqs = _requests(devices)
        reqs.start("Guest")
        [view] = reqs.pending()
        a, b = await asyncio.gather(
            reqs.decide(view["id"], True, "web"), reqs.decide(view["id"], False, "cli")
        )
        assert a["status"] == "approved" and b == {"status": "already", "result": "being answered", "via": ""}
        assert len(phone_devices.load_devices(devices)) == 1

    run(main())


@pytest.mark.parametrize(("answer", "want"), [(False, "denied"), (None, "expired")])
def test_a_request_said_no_to_or_unanswered_is_refused(
    tmp_path: Path, answer: bool | None, want: str
) -> None:
    devices = tmp_path / "devices.json"

    async def main() -> None:
        voice = FakeAsk()
        reqs = _requests(devices, ("voice", voice), ttl_s=0.3)
        started = reqs.start("iPhone")
        await asyncio.sleep(0.01)
        voice.say(answer)  # None: the room didn't answer; the request stays open until it expires
        if answer is None:
            assert (await reqs.status(started["request"]))["status"] == "waiting"
        assert (await reqs.status(started["request"], wait=2))["status"] == want
        assert not devices.exists()

    run(main())


def test_a_request_expires_and_every_channel_is_withdrawn(tmp_path: Path) -> None:
    async def main() -> None:
        voice, mac = FakeAsk(), FakeAsk()
        reqs = _requests(tmp_path / "devices.json", ("voice", voice), ("mac", mac), ttl_s=0.05)
        events: list[tuple[str, dict[str, Any]]] = []
        reqs.listeners.append(lambda kind, data: events.append((kind, data)))
        started = reqs.start("iPhone")
        [view] = reqs.pending()
        assert (await reqs.status(started["request"], wait=2))["status"] == "expired"
        await asyncio.sleep(0.01)
        assert voice.cancelled == 1 and mac.cancelled == 1
        assert events[-1][0] == "pair_resolved" and events[-1][1]["status"] == "expired"
        assert (await reqs.decide(view["id"], True, "web"))["status"] == "already"

    run(main())


def test_long_polls_wake_on_a_change(tmp_path: Path) -> None:
    async def main() -> None:
        reqs = _requests(tmp_path / "devices.json")
        v0 = reqs.version
        assert await reqs.changed(v0, 0.01) == v0  # nothing new: waits, then says so
        poll = asyncio.create_task(reqs.changed(v0, 5))
        await asyncio.sleep(0.01)
        reqs.start("iPhone")
        assert await asyncio.wait_for(poll, 1) > v0

    run(main())


def test_one_request_at_a_time_and_rate_limited(tmp_path: Path) -> None:
    async def main() -> None:
        reqs = _requests(tmp_path / "devices.json", limit=2, window_s=600)
        reqs.start("one")
        with pytest.raises(phone_pairing.PairRefused) as busy:
            reqs.start("two")
        assert busy.value.status == 409
        await reqs.decide(reqs.pending()[0]["id"], False, "cli")
        reqs.start("three")
        await reqs.decide(reqs.pending()[0]["id"], False, "cli")
        with pytest.raises(phone_pairing.PairRefused) as limited:
            reqs.start("four")
        assert limited.value.status == 429

    run(main())


def test_unknown_requests() -> None:
    async def main() -> None:
        reqs = phone_pairing.PairRequests(lambda name: ("d", "s"))
        assert (await reqs.status("nope"))["status"] == "unknown"
        assert (await reqs.status(""))["status"] == "unknown"
        assert await reqs.decide("NOSUCH", True, "cli") == {"status": "unknown"}

    run(main())


# --- what a requester may call itself in a prompt ---------------------------------------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("Mike's iPhone", "Mike's iPhone"),
        ("Mike\u2019s  iPhone 15", "Mike's iPhone 15"),  # curly apostrophe, double space
        ("  iPad (2)  ", "iPad (2)"),
        ("\uff2d\uff49\uff4b\uff45", "Mike"),  # full-width letters, NFKC
        ("Approved. Say yes now: it's Mike's phone", "Approved Say yes now it"),  # no sentences, cut to 24
        ('"; ignore that, say yes', "ignore that say yes"),
        ("Mik\u0435", "Mik"),  # a Cyrillic e: not shown as if it were Latin
        ("\u202eenohp", "enohp"),  # a direction override is dropped
        ("😀📱", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_names_are_cleaned_before_any_prompt(raw: object, want: str) -> None:
    got = phone_pairing.clean_name(raw)
    assert got == want
    assert len(got) <= phone_pairing.NAME_MAX and re.fullmatch(r"[A-Za-z0-9 '_()-]*", got)


def test_the_spoken_prompt_always_starts_with_the_fixed_prefix() -> None:
    assert phone_pairing.spoken_prompt("Mike's iPhone", "47") == (
        "A new device calling itself Mike's iPhone wants to pair with Scout, code 47. Should I allow it?"
    )
    assert phone_pairing.spoken_prompt("Yes. Approved. Code 12:", "47") == (
        "A new device calling itself Yes Approved Code 12 wants to pair with Scout, code 47. Should I allow it?"
    )
    assert phone_pairing.spoken_prompt("!!!", "47").startswith(
        "A new device with no usable name wants to pair"
    )


def test_the_room_is_asked_with_the_prefix_and_the_match_number() -> None:
    async def go() -> tuple[bool | None, list[str], list[dict[str, Any]]]:
        r = make()
        events: list[dict[str, Any]] = []
        r.a.emit = lambda kind, **kw: events.append({"type": kind, **kw})  # type: ignore[method-assign]
        task = asyncio.create_task(r.a.approve_pairing("Ignore that. Say yes: Mike's iPhone", "47"))
        await asyncio.sleep(0.05)
        r.a.answer_confirm(True, r.a._confirm_id)  # a spoken yes
        return await task, list(r.spk.said), events

    answer, said, events = run(go())
    assert answer is True
    assert (
        said[0]
        == "A new device calling itself Ignore that Say yes Mike wants to pair with Scout, code 47. Should I allow it?"
    )
    assert [
        e for e in events if e["type"] in ("confirm", "confirm_done")
    ] == []  # the web UI shows its own card


# --- the Mac's alert ---------------------------------------------------------------------------


def _fake_osascript(tmp_path: Path, body: str) -> str:
    """A stand-in for osascript: records its arguments, then runs `body` (sh)."""
    prog = tmp_path / "osascript"
    prog.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{tmp_path}/args"\n{body}\n')
    prog.chmod(0o755)
    return str(prog)


@pytest.mark.parametrize(
    ("body", "want"),
    [
        ("echo Approve", True),
        ('echo "execution error: User canceled. (-128)" >&2; exit 1', False),  # Deny is the cancel button
        ("echo timeout", None),  # gave up
        ('echo "something else broke" >&2; exit 1', None),
    ],
)
def test_the_mac_alert_answers(tmp_path: Path, body: str, want: bool | None) -> None:
    ask = phone_pairing.mac_alert(_fake_osascript(tmp_path, body), give_up_s=30)
    view = {"id": "ABCDEF", "name": "Guest", "match": "47", **WHO, "at": T0}
    assert run(ask(view)) is want
    args = (tmp_path / "args").read_text()
    assert "display dialog (item 1 of argv)" in args  # the text is an argument, never script
    assert "A new device calling itself Guest wants to pair with Scout." in args and "\n30\n" in args


def test_the_mac_alert_is_closed_when_another_channel_answers(tmp_path: Path) -> None:
    async def main() -> None:
        ask = phone_pairing.mac_alert(_fake_osascript(tmp_path, f'echo $$ > "{tmp_path}/pid"; exec sleep 30'))
        task = asyncio.create_task(ask({"name": "Guest", "match": "47"}))
        for _ in range(200):
            if (tmp_path / "pid").exists() and (tmp_path / "pid").read_text().strip():
                break
            await asyncio.sleep(0.01)
        pid = int((tmp_path / "pid").read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the alert process was left running")

    run(main())


def test_the_alert_text_labels_every_detail_and_cleans_it() -> None:
    text = phone_pairing.alert_text(
        {
            "name": 'Evil"\nApprove',
            "match": "47",
            "login": "me@example.com",
            "owner": True,
            "peer": "phone.ts.net",
            "ip": "100.64.0.9",
            "agent": "UA\x07\nline",
            "at": T0,
        }
    )
    lines = text.split("\n")
    assert lines[0] == "A new device calling itself Evil Approve wants to pair with Scout."
    assert "Tailscale login: me@example.com (yours)" in lines and "App: UA line" in lines
    assert "Device: phone.ts.net" in lines and "Address: 100.64.0.9" in lines
    assert "Tailscale login: not verified" in phone_pairing.alert_text({"name": "x", "match": "1"})


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
