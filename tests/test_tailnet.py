"""tailnet.py: this Mac's tailnet name and addresses, and who a peer is, from tailscaled."""

from __future__ import annotations

import json

from scout import tailnet

STATUS = {
    "Self": {
        "DNSName": "Mini.tail1234.ts.net.",
        "TailscaleIPs": ["100.85.0.1", "fd7a:115c:a1e0::1", "10.0.0.1"],
        "UserID": 7,
    },
    "User": {"7": {"LoginName": "mike@example.com"}},
}


class FakeCli:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        return self.answers.get(" ".join(args), "")


def test_this_macs_name_addresses_and_owner() -> None:
    me = tailnet.self_info(STATUS)
    assert me == tailnet.SelfInfo(
        "mini.tail1234.ts.net", frozenset({"100.85.0.1", "fd7a:115c:a1e0::1"}), "mike@example.com"
    )
    assert tailnet.self_info({}) == tailnet.SelfInfo()
    assert tailnet.self_info(None) == tailnet.SelfInfo()
    assert tailnet.owner_login({"Self": {"UserID": 9}, "User": {}}) == ""


def test_a_peer_is_who_tailscaled_says() -> None:
    user = {"Node": {"Name": "phone.tail1234.ts.net."}, "UserProfile": {"LoginName": "mike@example.com"}}
    assert tailnet.peer_of(user) == tailnet.Peer("mike@example.com", "phone.tail1234.ts.net")
    tagged = {
        "Node": {"Name": "srv.ts.net.", "Tags": ["tag:server"]},
        "UserProfile": {"LoginName": "tagged-devices"},
    }
    assert tailnet.peer_of(tagged) == tailnet.Peer("", "srv.ts.net")  # a tagged device has no user
    assert tailnet.peer_of({}) is None and tailnet.peer_of(None) is None and tailnet.peer_of([]) is None


def test_answers_are_cached_and_refreshes_rate_limited() -> None:
    now = [1000.0]
    cli = FakeCli({"status --json": json.dumps(STATUS), "whois --json 100.64.0.9": json.dumps(
        {"Node": {"Name": "p."}, "UserProfile": {"LoginName": "a@example.com"}})})  # fmt: skip
    tn = tailnet.Tailnet(run=cli, clock=lambda: now[0])
    assert tn.me().name == "mini.tail1234.ts.net"
    assert tn.me().name and tn.me(refresh=True).name  # a refresh right away isn't another read
    assert cli.calls == [["status", "--json"]]
    now[0] += tailnet.REFRESH_MIN_S + 1
    tn.me(refresh=True)
    assert len(cli.calls) == 2
    assert tn.whois("100.64.0.9") == tailnet.Peer("a@example.com", "p")
    tn.whois("100.64.0.9")
    assert cli.calls.count(["whois", "--json", "100.64.0.9"]) == 1
    assert tn.whois("192.168.1.9") is None and tn.whois("not an ip") is None  # never asked
    assert tn.whois("100.64.0.10") is None  # tailscaled didn't know it
    assert ["whois", "--json", "192.168.1.9"] not in cli.calls


def test_tailscaled_not_answering_keeps_what_was_known() -> None:
    now = [0.0]
    cli = FakeCli({"status --json": json.dumps(STATUS)})
    tn = tailnet.Tailnet(run=cli, clock=lambda: now[0])
    assert tn.me().owner == "mike@example.com"
    cli.answers.clear()
    now[0] += tailnet.STATUS_TTL_S + 1
    assert tn.me().name == "mini.tail1234.ts.net"


def test_is_tailnet_ip() -> None:
    assert tailnet.is_tailnet_ip("100.64.0.1") and tailnet.is_tailnet_ip("fd7a:115c:a1e0::5")
    assert (
        not tailnet.is_tailnet_ip("127.0.0.1")
        and not tailnet.is_tailnet_ip("")
        and not tailnet.is_tailnet_ip(None)
    )
