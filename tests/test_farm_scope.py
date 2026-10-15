"""What the task farm reads for tasks and keeps (taskfarm.py, docs/tasks-design.md "What
involves you"): Slack conversations the user is in or addressed in, owners asked by or
of the user, mail sent to the user, and the once-over of open Slack tasks found before.
Synthetic conversations and mail only."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from scout import config, index, reminders_mac, taskfarm, tasks

NOW = time.time()
ME = taskfarm.SlackMe("UME", ("mike j", "mike"))


def ts(hours_ago: float) -> str:
    return f"{NOW - hours_ago * 3600:.6f}"


@pytest.fixture
def s(monkeypatch: pytest.MonkeyPatch) -> index.Index:
    async def no_reminders(helper: Any = None) -> bool:
        return False

    monkeypatch.setattr(reminders_mac, "available", no_reminders)
    monkeypatch.setattr(tasks, "_people", lambda: [])
    monkeypatch.setattr(taskfarm, "slack_me", lambda: ME)
    st = index.default()
    assert isinstance(st, index.Index)
    return st


def put(
    st: index.Index,
    ch: str,
    title: str,
    t: str,
    lines: list[tuple[str, str, str, list[str]]],
    bot: bool = False,
) -> None:
    """A Slack item as slack.documents stores it: lines of (uid, name, text, mentioned uids)."""
    body = "\n".join(f"{name}: {text}" for _, name, text, _ in lines)
    meta = {"source": "slack", "channel": ch, "conversation": title, "ts": t, "last_ts": t, "bot": bot,
            "lines": [[uid, list(at)] for uid, _, _, at in lines]}  # fmt: skip
    st.put(f"slack:{ch}:{t}", "slack", title=title, body=body, who=lines[0][1], ts=float(t), meta=meta)


PAT = ("U1", "Pat Doe")
LUCA = ("U2", "Luca")
MIKE = ("UME", "Mike J")


def say(who: tuple[str, str], text: str, at: list[str] | None = None) -> tuple[str, str, str, list[str]]:
    return (who[0], who[1], text, at or [])


class Fake:
    """The local model: tasks for whichever conversation names a key word."""

    def __init__(self, answers: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.answers = answers or {}
        self.read: list[str] = []

    async def __call__(self, system: str, text: str) -> str:
        self.read.append(text)
        found = [t for word, ts_ in self.answers.items() if word in text for t in ts_]
        return json.dumps({"tasks": found})


def farm(st: index.Index, local: Fake, tmp_path: Path) -> dict[str, int]:
    return asyncio.run(taskfarm.farm_pass(st, Fake(), local, now=NOW, state_path=tmp_path / "farm.json"))


def titles(st: index.Index, status: str = "open") -> set[str]:
    return {t.title for t in tasks.all_tasks(st) if t.status == status}


# --- what is read -----------------------------------------------------------------------------


def test_channel_chatter_between_others_is_never_read(s: index.Index, tmp_path: Path) -> None:
    put(s, "C1", "#eng", ts(5), [say(PAT, "Luca, can you follow up on ticket 61723?"),
                                 say(LUCA, "Sure, I'll follow up with support tomorrow")])  # fmt: skip
    put(s, "C1", "#eng", ts(4), [say(PAT, "please review the deploy doc", ["U2"])])
    local = Fake()
    c = farm(s, local, tmp_path)
    assert local.read == [] and c["read"] == 0 and c["skipped"] >= 1


@pytest.mark.parametrize(
    "ch, title, lines",
    [
        ("D1", "DM with Pat Doe", [say(PAT, "can you send the deck by Friday?")]),
        ("G1", "Group DM with pat, luca, mike", [say(PAT, "can you send the deck by Friday?")]),
        ("C1", "#eng", [say(PAT, "<@UME> can you send the deck by Friday?", ["UME"])]),
        ("C1", "#eng", [say(PAT, "Mike, can you send the deck by Friday?")]),  # by name, no @
        (
            "C1",
            "#eng",
            [say(PAT, "who has the deck?"), say(MIKE, "I'll send the deck by Friday")],
        ),  # own thread
        ("C1", "#eng", [say(MIKE, "I'll send the deck by Friday")]),  # own commitment
    ],
)
def test_what_involves_the_user_is_read(
    s: index.Index, tmp_path: Path, ch: str, title: str, lines: list[tuple[str, str, str, list[str]]]
) -> None:
    put(s, ch, title, ts(3), lines)
    local = Fake()
    assert farm(s, local, tmp_path)["read"] == 1 and len(local.read) == 1 and "deck" in local.read[0]


def test_a_mention_of_someone_else_is_not_the_user(s: index.Index, tmp_path: Path) -> None:
    put(s, "C1", "#eng", ts(3), [say(PAT, "@Luca can you send the deck by Friday?", ["U2"])])
    local = Fake()
    assert farm(s, local, tmp_path)["read"] == 0 and local.read == []


def test_without_knowing_who_the_user_is_only_dms_are_read(
    s: index.Index, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(taskfarm, "slack_me", lambda: taskfarm.SlackMe("", ()))
    put(s, "C1", "#eng", ts(3), [say(PAT, "Mike, can you send the deck?")])
    put(s, "D1", "DM with Pat Doe", ts(3), [say(PAT, "can you send the budget?")])
    local = Fake()
    farm(s, local, tmp_path)
    assert len(local.read) == 1 and "budget" in local.read[0]


def test_old_items_without_line_ids_go_by_names(s: index.Index, tmp_path: Path) -> None:
    """Stored before items carried who wrote each line: speaker labels and @names."""
    for i, (body, involved) in enumerate([
        ("Pat Doe: @Mike J can you send the deck?", True),
        ("Mike J: I'll send the budget", True),
        ("Pat Doe: Luca, can you send the logs?\nLuca: sure", False),
    ]):  # fmt: skip
        t = ts(5 - i)
        s.put(f"slack:C{i}:{t}", "slack", title=f"#c{i}", body=body, who="x", ts=float(t),
              meta={"source": "slack", "channel": f"C{i}", "conversation": f"#c{i}", "ts": t, "last_ts": t})  # fmt: skip
        h = s.get(f"slack:C{i}:{t}")
        assert h is not None and taskfarm.slack_involves(h, ME) is involved, body


# --- whose tasks are kept ---------------------------------------------------------------------


def task(title: str, owner: str, evidence: str) -> dict[str, Any]:
    return {"title": title, "owner": owner, "due": None, "evidence": evidence}


def test_owners_need_the_user_to_have_asked_or_been_asked(s: index.Index, tmp_path: Path) -> None:
    # A thread the user wrote in: the user is in it, but Pat and Luca's agreement isn't theirs.
    put(s, "C1", "#eng", ts(5), [
        say(MIKE, "Luca, can you send me the logs?"),
        say(LUCA, "I'll send you the logs tonight"),
        say(PAT, "Luca, can you follow up on ticket 61723?"),
        say(LUCA, "Sure, I'll follow up on it tomorrow"),
        say(PAT, "and can someone update the wiki page?"),
        say(MIKE, "I'll review the wiki change"),
        say(PAT, "Luca, can you rotate the staging keys?"),  # after the user's line, not an answer to it
    ])  # fmt: skip
    local = Fake({"#eng": [
        task("Get the logs from Luca", "Luca", "I'll send you the logs tonight"),  # asked by the user
        task("Follow up with Luca about ticket 61723", "Luca", "Sure, I'll follow up on it tomorrow"),
        task("Follow up on ticket 61723", "me", "can you follow up on ticket 61723?"),  # asked of Luca
        task("Update the wiki page", "me", "can someone update the wiki page?"),  # not addressed
        task("Review the wiki change", "me", "I'll review the wiki change"),  # the user's own
        task("Rotate the staging keys", "Luca", "can you rotate the staging keys?"),  # Pat asked Luca
    ]})  # fmt: skip
    farm(s, local, tmp_path)
    assert titles(s) == {"Get the logs from Luca", "Review the wiki change"}


def test_in_a_dm_everything_is_the_users(s: index.Index, tmp_path: Path) -> None:
    put(s, "D1", "DM with Pat Doe", ts(3), [say(PAT, "can you send the deck? and Luca will fix the build")])
    local = Fake({"DM with": [task("Send the deck", "me", "can you send the deck?"),
                              task("Fix the build", "Luca", "Luca will fix the build")]})  # fmt: skip
    farm(s, local, tmp_path)
    assert titles(s) == {"Send the deck", "Fix the build"}


def test_in_a_group_dm_the_users_tasks_are_kept_others_need_the_user(s: index.Index, tmp_path: Path) -> None:
    put(s, "G1", "Group DM with pat, luca, mike", ts(3), [
        say(PAT, "can you send the deck?"),
        say(PAT, "Luca, can you fix the build?"),
        say(LUCA, "yes, I'll fix the build today"),
    ])  # fmt: skip
    local = Fake({"Group DM": [task("Send the deck", "me", "can you send the deck?"),
                               task("Fix the build", "Luca", "yes, I'll fix the build today")]})  # fmt: skip
    farm(s, local, tmp_path)
    assert titles(s) == {"Send the deck"}


# --- tasks found before the rule ----------------------------------------------------------------


def old_task(
    st: index.Index, title: str, owner: str, source: str, conv: str, evidence: str, kind: str = "slack"
) -> int:
    label = f"the Slack conversation {conv!r} on Oct 14" if kind == "slack" else "the email 'x' from Pat"
    ctx = f'From {label}: "{evidence}" ({source})'
    return st.task_add(title, {"status": "open", "owner": owner, "source": source, "from": kind,
                            "reminder": f"x-apple-reminder://{title[:4]}"}, ctx)  # fmt: skip


def test_open_slack_tasks_are_checked_once_against_the_rule(
    s: index.Index, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    done: list[str] = []

    async def complete(a: dict[str, Any], mirror: bool = False) -> str:
        done.append(str(a["id"]))
        return "ok"

    monkeypatch.setattr(reminders_mac, "complete", complete)
    t5, t4, t3 = ts(5), ts(4), ts(3)
    put(s, "C1", "#eng", t5, [say(PAT, "Luca, can you follow up on ticket 61723?"),
                              say(LUCA, "Sure, I'll follow up on it tomorrow")])  # fmt: skip
    put(s, "C1", "#eng", t4, [say(PAT, "Mike, can you send the deck?")])
    put(s, "D1", "DM with Pat Doe", t3, [say(PAT, "please call the vendor")])
    chatter = old_task(s, "Follow up with Luca about ticket 61723", "me", f"slack:C1@{t5}", "#eng",
                       "Sure, I'll follow up on it tomorrow")  # fmt: skip
    asked = old_task(s, "Send the deck", "me", f"slack:C1@{t4}", "#eng", "Mike, can you send the deck?")
    dm = old_task(s, "Call the vendor", "me", f"slack:D1@{t3}", "DM with Pat Doe", "please call the vendor")
    gone = old_task(s, "Ship it", "me", "slack:C9@1.000000", "#gone", "ship it")  # its messages are gone
    mail = old_task(s, "Reply to Pat", "me", "mail:7", "", "can you reply", kind="mail")

    dry = asyncio.run(taskfarm.recheck_slack_tasks(s, ME, dry_run=True))
    assert dry == {"checked": 4, "dropped": 1, "kept": 2, "unknown": 1}
    assert titles(s, "dropped") == set() and done == []

    state = tmp_path / "farm.json"
    assert asyncio.run(taskfarm.recheck_once(s, ME, state)) == dry
    assert titles(s, "dropped") == {"Follow up with Luca about ticket 61723"}
    h = s.get(f"task:{chatter}")
    assert h is not None and "Dropped: from a channel conversation that didn't involve you" in h.body
    assert done == ["x-apple-reminder://Foll"]  # its reminder completed, like task_drop
    for n in (asked, dm, gone, mail):
        h = s.get(f"task:{n}")
        assert h is not None and tasks.of(h).status == "open"
    assert asyncio.run(taskfarm.recheck_once(s, ME, state)) is None  # once
    # Reversible: open again by hand, and it's left alone.
    s.task_update(chatter, {"status": "open"})
    assert asyncio.run(taskfarm.recheck_once(s, ME, state)) is None


def test_the_once_over_waits_until_the_users_slack_id_is_known(s: index.Index, tmp_path: Path) -> None:
    assert asyncio.run(taskfarm.recheck_once(s, taskfarm.SlackMe("", ("mike",)), tmp_path / "f.json")) is None
    assert "slack_scope_checked" not in taskfarm.load_state(tmp_path / "f.json")


# --- mail ---------------------------------------------------------------------------------------


def me_is(monkeypatch: pytest.MonkeyPatch, me: list[str]) -> None:
    cfg = config.Config()
    cfg.tasks.me = me
    for mod in (config, tasks, taskfarm):
        monkeypatch.setattr(mod, "load", lambda: cfg)


@pytest.fixture
def me_by_mail(monkeypatch: pytest.MonkeyPatch) -> None:
    me_is(monkeypatch, ["Mike Jerris", "mike@example.com"])


def mail_hit(to: list[str] | None, cc: list[str], body: str) -> index.Hit:
    meta: dict[str, Any] = {} if to is None else {"to": to, "cc": cc}
    return index.Hit("mail:1", "mail", "Q3 plan", body, "Pat <pat@example.com>", NOW, meta)


@pytest.mark.parametrize(
    "to, cc, body, read",
    [
        (["mike@example.com"], [], "Can you review the plan by Friday?", True),
        (["Mike@Example.com"], ["sam@example.com"], "Can you review the plan by Friday?", True),
        (["sam@example.com"], ["mike@example.com"], "Sam, can you review the plan by Friday?", False),
        (["sam@example.com"], ["mike@example.com"], "Sam, Mike: can you review the plan by Friday?", True),
        (["team@lists.example.com"], [], "Please review the plan by Friday.", False),
        (["team@lists.example.com"], [], "Mike, please review the plan by Friday.", True),
        (None, [], "Can you review the plan by Friday?", True),  # stored before To was kept
    ],
)
def test_mail_is_read_when_it_was_sent_to_the_user_or_names_them(
    me_by_mail: None, to: list[str] | None, cc: list[str], body: str, read: bool
) -> None:
    assert taskfarm.worth_reading(mail_hit(to, cc, body)) is read


def test_mail_without_the_users_address_known_is_read_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    me_is(monkeypatch, ["Mike Jerris"])
    assert taskfarm.worth_reading(mail_hit(["sam@example.com"], [], "Can you review the plan by Friday?"))
