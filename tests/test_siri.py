"""Siri on a paired phone (docs/siri-design.md): the phone's two keys, what each may do,
and the signed endpoints that give out the second key and take a question."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from test_phone_push import ALERT, VOIP, _signed, devices, token_file  # noqa: F401 (fixtures)
from test_routing import run
from test_web import StubApp, _phone, serve

from scout import phone_devices, phone_push
from scout import web as web_mod

ADDRESS = "/public/scout"


def _uv(device: str, secret: str, nonce: str, address: str = ADDRESS, key: str = "") -> dict[str, Any]:
    ts = int(time.time())
    uv = {
        "device": device,
        "ts": ts,
        "nonce": nonce,
        "sig": phone_devices.sign(secret, device, ts, nonce, address),
    }
    return {**uv, "key": key} if key else uv


def test_each_key_does_only_what_its_config_allows(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    device, secret = phone_devices.pair("Mike iPhone", path)
    check = phone_devices.CallCheck(ADDRESS, 60, path)
    assert check.check(_uv(device, secret, "nonce-0001", key="locked")).reason == "no locked key"
    locked = phone_devices.new_locked_key(device, path)
    assert phone_devices.listing(path)[0]["locked_key"] is True

    v = check.check(_uv(device, locked, "nonce-0002", key="locked"))  # a call: not by default
    assert not v.ok and v.reason == "the locked key may not call"
    v = check.check(_uv(device, locked, "nonce-0003", key="locked"), need="ask")
    assert v.ok and v.key == "locked"
    assert check.check(_uv(device, secret, "nonce-0004")).ok  # the pairing key calls, as before
    # Each key signs only as itself.
    assert check.check(_uv(device, secret, "nonce-0005", key="locked"), need="ask").reason == "bad signature"
    assert check.check(_uv(device, secret, "nonce-0006", key="spare")).reason == "no such key"

    custom = phone_devices.CallCheck(
        ADDRESS, 60, path, abilities=phone_devices.abilities(["call", "push"], ["ask", "call"])
    )
    assert custom.check(_uv(device, locked, "nonce-0007", key="locked")).ok  # configured to call
    assert (
        custom.check(_uv(device, secret, "nonce-0008"), need="ask").reason == "the unlocked key may not ask"
    )

    again = phone_devices.new_locked_key(device, path)  # a new one replaces the old
    assert check.check(_uv(device, locked, "nonce-0009", key="locked"), need="ask").reason == "bad signature"
    assert check.check(_uv(device, again, "nonce-0010", key="locked"), need="ask").ok
    assert phone_devices.forget(device, path)  # forgetting the phone ends both
    assert check.check(_uv(device, again, "nonce-0011", key="locked"), need="ask").reason == "device revoked"


def test_an_ability_that_isnt_one_is_refused() -> None:
    with pytest.raises(ValueError, match="locked_key: send"):
        phone_devices.abilities(["call"], ["ask", "send"])


class SlowApp(StubApp):
    """Answers the first ask late (Siri stops waiting), then from what it kept."""

    async def ask(self, agent: str, text: str, deadline: float = 20.0, via: str = "") -> dict[str, Any]:
        self.calls.append(("ask", agent, text, deadline))
        if len([c for c in self.calls if c[0] == "ask"]) == 1:
            return {"status": "pending", "text": "I'm still working on that."}
        return {"status": "ok", "text": "The dealer closes at 6.", "tier": "claude"}


def test_siri_asks_with_the_locked_key_signed_for_its_text(token_file: Path, devices: Path) -> None:  # noqa: F811
    device, secret = phone_devices.pair("Mike iPhone", devices)

    async def main() -> None:
        async with serve(phone=_phone()) as srv, aiohttp.ClientSession() as s:
            key_url, ask_url = srv.url + "/api/phone/key", srv.url + "/api/phone/ask"
            got = _signed(device, secret, web_mod.LOCKED_KEY_PURPOSE, "nonce-0001")
            async with s.post(key_url, json=got) as r:
                assert r.status == 200
                body = await r.json()
                assert body["key"] == "locked" and body["abilities"] == ["ask"]
                locked = body["secret"]
            # The locked key can't get itself a new key.
            async with s.post(key_url, json={**_signed(device, locked, web_mod.LOCKED_KEY_PURPOSE, "nonce-0002"),
                                             "key": "locked"}) as r:  # fmt: skip
                assert r.status == 401

            q = "what's the weather tomorrow"
            ask = {**_signed(device, locked, web_mod.ask_purpose(q), "nonce-0003", text=q), "key": "locked"}
            async with s.post(ask_url, json=ask) as r:
                assert (r.status, await r.json()) == (200, {"status": "ok", "text": "It's sunny."})
            assert srv.app.calls[-1] == ("ask", "siri", q)
            # Signed for another question: refused.
            other = {**_signed(device, locked, web_mod.ask_purpose("anything else"), "nonce-0004", text=q),
                     "key": "locked"}  # fmt: skip
            async with s.post(ask_url, json=other) as r:
                assert r.status == 401

    run(main())


def test_a_key_configured_not_to_ask_cant(token_file: Path, devices: Path) -> None:  # noqa: F811
    device, secret = phone_devices.pair("Mike iPhone", devices)
    locked = phone_devices.new_locked_key(device, devices)
    q = "what should I work on"

    async def main() -> None:
        async with serve(phone=_phone(locked_key=[])) as srv, aiohttp.ClientSession() as s:
            ask = {**_signed(device, locked, web_mod.ask_purpose(q), "nonce-0001", text=q), "key": "locked"}
            async with s.post(srv.url + "/api/phone/ask", json=ask) as r:
                assert r.status == 401
            mine = _signed(device, secret, web_mod.ask_purpose(q), "nonce-0002", text=q)
            async with s.post(srv.url + "/api/phone/ask", json=mine) as r:
                assert r.status == 200

    run(main())


def test_a_slow_answer_comes_as_a_notification(
    token_file: Path,  # noqa: F811
    devices: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device, secret = phone_devices.pair("Mike iPhone", devices)
    phone_devices.set_push(device, VOIP, ALERT, "sandbox", devices)
    sent: list[phone_push.Push] = []
    done = None

    async def send(pushes: list[phone_push.Push]) -> list[phone_push.Result]:
        sent.extend(pushes)
        assert done is not None
        done.set()
        return [phone_push.Result(p, 200) for p in pushes]

    monkeypatch.setattr(phone_push, "problem", lambda cfg: None)
    monkeypatch.setattr(phone_push, "sender", lambda cfg, devices=None: send)
    q = "how far is the dealer"

    async def main() -> None:
        nonlocal done
        import asyncio

        done = asyncio.Event()
        async with serve(SlowApp(), phone=_phone()) as srv, aiohttp.ClientSession() as s:
            mine = _signed(device, secret, web_mod.ask_purpose(q), "nonce-0001", text=q)
            async with s.post(srv.url + "/api/phone/ask", json=mine) as r:
                assert await r.json() == {
                    "status": "pending",
                    "text": "Still working on it. I'll send the answer to your phone.",
                }
            await asyncio.wait_for(done.wait(), 5)
            assert [c[3] for c in srv.app.calls] == [web_mod.SIRI_DEADLINE_S, web_mod.SIRI_KEEP_S]

    run(main())
    assert [(p.device, p.kind, p.token) for p in sent] == [(device, "alert", ALERT)]
    assert sent[0].payload["aps"]["alert"] == {
        "title": "Scout",
        "subtitle": q,
        "body": "The dealer closes at 6.",
    }
