"""Contacts: the scout-contacts helper (native/contacts) built and run against a fixture
file (--fixture: it never opens the Contacts store, so no permission prompt), its Python
client, the privacy views, and the first-use access flow through a fake helper that
records its arguments. Nothing here reads the user's real contacts."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from scout import contacts_mac, privacy
from scout.contacts_mac import Helper
from scout.mac import ToolError

ROOT = Path(__file__).resolve().parents[1]

PEOPLE: dict[str, Any] = {
    "contacts": [
        {
            "id": "c1",
            "given": "Pat",
            "family": "Lee",
            "organization": "Acme",
            "job_title": "Engineer",
            "phones": [{"label": "mobile", "value": "+1 (555) 010-2000"}, {"label": "work", "value": "555-010-3000"}],
            "emails": [{"label": "work", "value": "pat@acme.example"}],
            "addresses": [{"label": "home", "street": "1 Elm St", "city": "Luther", "state": "MI", "postal_code": "49656", "country": "USA"}],
            "birthday": {"month": 3, "day": 14},
            "groups": ["g1"],
        },
        {
            "id": "c2",
            "given": "Patricia",
            "family": "Moss",
            "phones": [{"label": "home", "value": "555 010 4000"}],
            "emails": [],
            "groups": ["g2"],
        },
        {
            "id": "c3",
            "given": "Robert",
            "family": "Diaz",
            "nickname": "Robbie",
            "emails": [{"label": "home", "value": "rob@example.com"}, {"label": "work", "value": "rdiaz@corp.example"}],
            "birthday": {"year": 1980, "month": 7, "day": 2},
            "groups": ["g2"],
        },
        {
            "id": "c4",
            "given": "Catherine",
            "family": "Öster",
            "phones": [{"label": "iPhone", "value": "+44 20 7946 0958"}],
            "groups": [],
        },
        {"id": "c5", "organization": "Northside Plumbing", "phones": [{"label": "work", "value": "555-010-9000"}]},
    ],
    "groups": [{"id": "g1", "name": "Work"}, {"id": "g2", "name": "Family"}],
}  # fmt: skip


def _tmp() -> Path:
    d = ROOT / ".tmp" / "contacts" / str(os.getpid())
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture(scope="module")
def binary() -> Iterator[Path]:
    if sys.platform != "darwin" or shutil.which("swiftc") is None:
        pytest.skip("needs macOS and swiftc")
    home = _tmp() / "home"
    r = subprocess.run(  # noqa: S603  our own build script
        [str(ROOT / "scripts" / "build-contacts.sh")],
        env={**os.environ, "SCOUT_HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert "warning:" not in r.stderr + r.stdout, r.stderr
    exe = home / "bin" / "scout-contacts"
    sig = subprocess.run(  # noqa: S603
        ["/usr/bin/codesign", "-dv", "--entitlements", "-", str(exe)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "Identifier=com.local.scout.contacts" in sig.stderr
    assert "runtime" in sig.stderr  # hardened runtime: no DYLD_* injection into the grant holder
    assert "personal-information.addressbook" in sig.stdout + sig.stderr
    yield exe
    shutil.rmtree(_tmp(), ignore_errors=True)


@pytest.fixture(scope="module")
def fixture_file(binary: Path) -> Path:
    f = _tmp() / "people.json"
    f.write_text(json.dumps(PEOPLE))
    return f


@pytest.fixture
def helper(binary: Path, fixture_file: Path) -> Helper:
    return Helper(binary=binary, args=["--fixture", str(fixture_file)])


def _cli(binary: Path, fixture: Path, *args: str) -> tuple[int, dict[str, Any]]:
    r = subprocess.run(  # noqa: S603  our own helper, on a fixture
        [str(binary), "--fixture", str(fixture), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return r.returncode, json.loads(r.stdout)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- the helper --------------------------------------------------------------------------


def test_the_prompt_names_scout_and_says_what_it_reads(binary: Path) -> None:
    text = (ROOT / "native" / "contacts" / "Info.plist").read_text()
    assert "<key>NSContactsUsageDescription</key>" in text
    assert "never changes a contact" in text
    assert b"NSContactsUsageDescription" in binary.read_bytes()  # embedded in the binary


def test_search_by_first_name_ranks_the_exact_name_first(binary: Path, fixture_file: Path) -> None:
    code, out = _cli(binary, fixture_file, "search", "--query", "pat")
    assert code == 0
    assert [p["id"] for p in out["contacts"]] == ["c1", "c2"]  # Pat exact, then Patricia (prefix)
    assert out["total"] == 2
    pat = out["contacts"][0]
    assert pat["name"] == "Pat Lee" and pat["organization"] == "Acme"
    assert pat["phones"][0] == {"label": "mobile", "value": "+1 (555) 010-2000"}
    assert pat["birthday"] == {"month": 3, "day": 14} and pat["groups"] == ["Work"]
    assert out["contacts"][0]["score"] < out["contacts"][1]["score"]


@pytest.mark.parametrize(
    ("query", "ids", "how"),
    [
        ("bob", ["c3"], "name"),  # a common nickname of Robert
        ("robbie", ["c3"], "nickname"),  # the contact's own nickname
        ("pat lee", ["c1"], "name"),
        ("lee", ["c1"], "name"),
        ("katherine", ["c4"], "name"),  # another spelling of Catherine
        ("dias", ["c3"], "similar"),  # one letter off: what speech recognition hears
        ("oster", ["c4"], "name"),  # accents folded
        ("northside", ["c5"], "organization"),  # a business card with no person's name
        ("acme", ["c1"], "organization"),
        ("pat@acme.example", ["c1"], "email"),
        ("RDIAZ@corp.example", ["c3"], "email"),
        ("5550102000", ["c1"], "phone"),  # +1 (555) 010-2000
        ("(555) 010-4000", ["c2"], "phone"),
        ("nobody", [], ""),
    ],
)
def test_search_is_fuzzy_ish(binary: Path, fixture_file: Path, query: str, ids: list[str], how: str) -> None:
    code, out = _cli(binary, fixture_file, "search", "--query", query)
    assert code == 0
    assert [p["id"] for p in out["contacts"]] == ids
    if ids:
        assert out["contacts"][0]["match"] == how


def test_search_by_group_and_limit(binary: Path, fixture_file: Path) -> None:
    _, fam = _cli(binary, fixture_file, "search", "--group", "family")
    assert [p["id"] for p in fam["contacts"]] == ["c2", "c3"]  # by name
    _, one = _cli(binary, fixture_file, "search", "--query", "pat", "--limit", "1")
    assert len(one["contacts"]) == 1 and one["total"] == 2
    code, bad = _cli(binary, fixture_file, "search", "--group", "nope")
    assert code == 1 and "groups: Family, Work" in bad["error"]
    code, bad = _cli(binary, fixture_file, "search", "--query", "x", "--limit", "99")
    assert code == 1 and "1-25" in bad["error"]
    code, bad = _cli(binary, fixture_file, "search")
    assert code == 1 and "required" in bad["error"]


def test_get_groups_and_resolve(binary: Path, fixture_file: Path) -> None:
    _, got = _cli(binary, fixture_file, "get", "--id", "c3")
    c = got["contact"]
    assert c["name"] == "Robert Diaz" and c["nickname"] == "Robbie"
    assert c["birthday"] == {"year": 1980, "month": 7, "day": 2}
    assert "notes" not in c
    code, missing = _cli(binary, fixture_file, "get", "--id", "zz")
    assert code == 1 and "no contact" in missing["error"]
    _, groups = _cli(binary, fixture_file, "groups")
    assert sorted((g["name"], g["count"]) for g in groups["groups"]) == [("Family", 2), ("Work", 1)]
    _, names = _cli(
        binary, fixture_file, "resolve", "--handle", "+15550102000", "--handle", "ROB@example.com",
        "--handle", "+15559999999",
    )  # fmt: skip
    assert names["names"] == {
        "+15550102000": {"name": "Pat Lee", "id": "c1"},
        "ROB@example.com": {"name": "Robert Diaz", "id": "c3"},
    }


def test_status_with_a_fixture_never_asks_macos(binary: Path, fixture_file: Path) -> None:
    assert _cli(binary, fixture_file, "status") == (0, {"status": "fixture"})
    assert _cli(binary, fixture_file, "request") == (0, {"granted": True, "status": "fixture"})
    code, out = _cli(binary, fixture_file, "edit", "--id", "c1")
    assert code == 1 and "unknown command" in out["error"]  # read-only: there is no write


# --- the Python client and privacy -------------------------------------------------------


def test_search_and_get_through_the_client(helper: Helper) -> None:
    data = run(contacts_mac.search_data("Pat's", helper=helper))  # "what's Pat's number"
    assert [p["id"] for p in data["contacts"]] == ["c1", "c2"]
    assert run(contacts_mac.contact_data("c1", helper))["name"] == "Pat Lee"
    with pytest.raises(ToolError, match="say who"):
        run(contacts_mac.search_data("", helper=helper))
    with pytest.raises(ToolError, match="limit"):
        run(contacts_mac.search_data("pat", 0, helper=helper))


def test_person_named_picks_the_clear_one(helper: Helper) -> None:
    assert [p["id"] for p in run(contacts_mac.person_named("pat", helper))] == ["c1"]  # exact first name wins
    assert [p["id"] for p in run(contacts_mac.person_named("Patricia Moss", helper))] == ["c2"]
    assert run(contacts_mac.person_named("nobody", helper)) == []


@pytest.mark.parametrize("mode", ["strict", "balanced", "open"])
def test_what_claude_sees_by_privacy_mode(helper: Helper, mode: str) -> None:
    pol = privacy.Policy(mode)
    found = run(pol.contacts_search("pat", helper=helper))
    one = run(pol.contact_get("c1", helper=helper))
    for out in (found, one):  # reaching someone: always
        assert "Pat Lee" in out and "+1 (555) 010-2000" in out and "pat@acme.example" in out
        assert "mobile" in out and "Engineer, Acme" in out and "id c1" in out
    if mode == "strict":
        assert "March 14" not in found + one and "Elm St" not in found + one
        assert "Addresses, birthdays and groups left out (strict privacy mode)" in one
    elif mode == "balanced":
        assert "birthday: March 14" in found and "Elm St" not in found
        assert "contact_get" in found  # how to get the address
        assert "address (home): 1 Elm St, Luther, MI 49656, USA" in one
    else:
        assert "Elm St" in found and "groups: Work" in found and "left out" not in found + one


def test_mail_recipients_by_name(helper: Helper) -> None:
    assert run(contacts_mac.addresses_for(["pat", "sam@example.com"], helper)) == [
        "pat@acme.example",
        "sam@example.com",
    ]
    with pytest.raises(ToolError, match="no email address"):
        run(contacts_mac.addresses_for(["Patricia"], helper))
    with pytest.raises(ToolError, match="no nobody in Contacts"):
        run(contacts_mac.addresses_for(["nobody"], helper))


def test_handles_and_names(helper: Helper) -> None:
    name, handles = run(contacts_mac.handles_for("Bob", helper))
    assert name == "Robert Diaz" and handles == ["rob@example.com", "rdiaz@corp.example"]
    assert run(contacts_mac.names_for(["+1 555 010 2000", "x@y.example"], helper)) == {
        "+1 555 010 2000": "Pat Lee"
    }


def test_spoken_numbers_come_in_groups() -> None:
    assert contacts_mac.spoken_number("+1 (555) 010-2000") == "5 5 5, 0 1 0, 2 0 0 0"
    assert contacts_mac.spoken_number("010-2000") == "0 1 0, 2 0 0 0"
    assert contacts_mac.spoken_number("+44 20 7946 0958") == "4 4 2, 0 7 9, 4 6 0, 9 5 8"


# --- first use, through a fake helper ----------------------------------------------------


def _fake(tmp: Path, status: str, granted: bool = True) -> Helper:
    log = tmp / "calls.jsonl"
    exe = tmp / "scout-contacts"
    reply = {"contacts": [{"id": "c1", "name": "Pat Lee", "phones": [], "emails": []}], "total": 1}
    exe.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "cmd = sys.argv[1]\n"
        f"if cmd == 'status': print(json.dumps({{'status': {status!r}}}))\n"
        f"elif cmd == 'request': print(json.dumps({{'granted': {granted!r}, 'status': 'x'}}))\n"
        "elif cmd == 'resolve': print(json.dumps({'names': {'+15550102000': {'name': 'Pat Lee', 'id': 'c1'}}}))\n"
        f"else: print({json.dumps(json.dumps(reply))})\n"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return Helper(binary=exe)


def _calls(tmp: Path) -> list[str]:
    return [json.loads(line)[0] for line in (tmp / "calls.jsonl").read_text().splitlines()]


def test_first_search_asks_for_access_once(tmp_path: Path) -> None:
    h = _fake(tmp_path, "not_determined")
    run(contacts_mac.search_data("pat", helper=h))
    run(contacts_mac.search_data("pat", helper=h))
    assert _calls(tmp_path) == ["status", "request", "search", "search"]


def test_refused_access_says_where_to_turn_it_on(tmp_path: Path) -> None:
    h = _fake(tmp_path, "denied")
    with pytest.raises(ToolError, match="Privacy and Security, Contacts"):
        run(contacts_mac.search_data("pat", helper=h))
    h = _fake(tmp_path, "not_determined", granted=False)
    with pytest.raises(ToolError, match="wasn't allowed"):
        run(contacts_mac.search_data("pat", helper=h))


def test_names_in_other_answers_never_ask(tmp_path: Path) -> None:
    h = _fake(tmp_path, "not_determined")
    assert run(contacts_mac.names_for(["+15550102000"], h)) == {}
    assert _calls(tmp_path) == ["status"]  # no request: a messages lookup doesn't prompt
    granted = tmp_path / "ok"
    granted.mkdir()
    h = _fake(granted, "authorized")
    assert run(contacts_mac.names_for(["+15550102000"], h)) == {"+15550102000": "Pat Lee"}
    assert run(contacts_mac.names_for(["+15550102000"], h)) == {"+15550102000": "Pat Lee"}
    assert _calls(granted) == ["status", "resolve"]  # remembered


def test_unbuilt_helper_and_turned_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = Helper(binary=tmp_path / "missing")
    with pytest.raises(ToolError, match="build-contacts"):
        run(contacts_mac.search_data("pat", helper=h))
    assert run(contacts_mac.names_for(["+15550102000"], h)) == {}
    from scout import config

    cfg = config.Config()
    cfg.contacts.enabled = False
    monkeypatch.setattr(contacts_mac, "load", lambda: cfg)
    with pytest.raises(ToolError, match=r"contacts\.enabled"):
        run(contacts_mac.search_data("pat", helper=_fake(tmp_path, "authorized")))


# --- tier 0 ------------------------------------------------------------------------------

PAT: dict[str, Any] = {
    "id": "c1", "name": "Pat Lee", "given": "Pat", "organization": "Acme",
    "phones": [{"label": "mobile", "value": "+1 (555) 010-2000"}, {"label": "work", "value": "555-010-3000"}],
    "emails": [{"label": "work", "value": "pat.lee@acme.example"}],
    "addresses": [{"label": "home", "street": "1 Elm St", "city": "Luther", "state": "MI", "postal_code": "49656"}],
    "birthday": {"month": 10, "day": 20, "year": 1990},
}  # fmt: skip
SAM: dict[str, Any] = {
    "id": "c9",
    "name": "Sam Ortiz",
    "phones": [],
    "emails": [],
    "birthday": {"month": 2, "day": 29},
}


def _tier0(people: dict[str, list[dict[str, Any]]]) -> tuple[Any, list[str]]:
    import datetime as dt

    from scout import local_intents
    from scout.tools import Timers

    asked: list[str] = []

    async def contacts(who: str) -> list[dict[str, Any]]:
        asked.append(who)
        return people.get(who, [])

    now = dt.datetime(2026, 10, 15, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=-4)))
    return local_intents.Context(Timers(lambda label: None), now=lambda: now, contacts=contacts), asked


def _say(c: Any, text: str) -> str | None:
    from scout import local_intents

    out: str | None = run(local_intents.answer(text, c))
    return out


@pytest.mark.parametrize(
    "said",
    ["What's Pat's number?", "what is pat's phone number", "tell me Pat's number", "Pat's number",
     "what's the phone number for Pat"],
)  # fmt: skip
def test_whats_pats_number(said: str) -> None:
    c, asked = _tier0({"pat": [PAT]})
    out = _say(c, said)
    assert (
        out == "Pat Lee's mobile number is 5 5 5, 0 1 0, 2 0 0 0, and work number is 5 5 5, 0 1 0, 3 0 0 0."
    )
    assert asked == ["pat"] and c.private is False


def test_a_kind_of_number_and_none_on_file() -> None:
    c, _ = _tier0({"pat": [PAT], "sam": [SAM]})
    assert _say(c, "what's pat's work number") == "Pat Lee's work number is 5 5 5, 0 1 0, 3 0 0 0."
    assert _say(c, "what's pat's home number") == "Pat Lee has no home phone number in your contacts."
    assert _say(c, "what's sam's number") == "Sam Ortiz has no phone number in your contacts."


def test_email_birthday_and_address() -> None:
    c, _ = _tier0({"pat": [PAT], "sam": [SAM], "pat lee": [PAT]})
    assert _say(c, "what's Pat's email") == "Pat Lee's work email is pat dot lee at acme dot example."
    assert (
        _say(c, "what's the email address for pat lee")
        == "Pat Lee's work email is pat dot lee at acme dot example."
    )
    out = _say(c, "when is Pat's birthday")
    assert out == "Pat Lee's birthday is October 20. That's in 5 days. They'll be 36."
    assert c.private is True  # not for Claude in strict mode
    assert _say(c, "when's sam's birthday") == "Sam Ortiz's birthday is February 29."
    assert _say(c, "what's pat's address") == "Pat Lee's home address is 1 Elm St, Luther, MI 49656."
    assert c.private is True


def test_unclear_or_unknown_names() -> None:
    other = {**PAT, "id": "c2", "name": "Pat Kim", "organization": ""}
    c, asked = _tier0({"pat": [PAT, other]})
    assert (
        _say(c, "what's pat's number")
        == "2 people in your contacts match Pat: Pat Lee (Acme) and Pat Kim. Which one?"
    )
    assert _say(c, "what's my wife's number") is None  # nobody called that: Claude takes it
    assert _say(c, "what's my number") is None and _say(c, "what's the number") is None
    assert asked == ["pat", "wife"]
    assert _say(c, "what's chris' number") is None and asked[-1] == "chris"  # a possessive s'


# --- mail: names for senders, names as recipients ----------------------------------------


class _Mail:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, script: str, *args: str) -> str:
        self.calls.append(args)
        return json.dumps({"drafted": True, "sent": True})


def test_mail_draft_to_a_name_and_send_wants_the_address(
    helper: Helper, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scout import mail_mac

    monkeypatch.setattr(contacts_mac, "HELPER", helper)
    fake = _Mail()
    out = run(mail_mac.draft({"to": ["Pat"], "cc": "rob@example.com", "subject": "Hi"}, run=fake))
    assert out.startswith("Draft to pat@acme.example saved")
    assert fake.calls[0][:2] == ("draft", "pat@acme.example")
    with pytest.raises(ToolError, match=r"From Contacts: Pat is pat@acme\.example"):
        run(mail_mac.send({"to": ["Pat"], "subject": "Hi"}, run=fake))
    with pytest.raises(ToolError, match="Patricia Moss has no email address"):
        run(mail_mac.draft({"to": ["Patricia"], "subject": "Hi"}, run=fake))
    assert len(fake.calls) == 1  # nothing sent


def test_bare_senders_get_their_names(helper: Helper, monkeypatch: pytest.MonkeyPatch) -> None:
    from scout import mail_mac

    monkeypatch.setattr(contacts_mac, "HELPER", helper)
    data = {
        "messages": [
            {"id": 1, "sender": "rob@example.com", "subject": "a"},
            {"id": 2, "sender": "Sam <sam@example.com>", "subject": "b"},
            {"id": 3, "sender": "stranger@example.com", "subject": "c"},
        ]
    }
    out = run(mail_mac.named_senders(data))
    assert [m["sender"] for m in out["messages"]] == [
        "Robert Diaz <rob@example.com>",
        "Sam <sam@example.com>",
        "stranger@example.com",
    ]
