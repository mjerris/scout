"""Tasks spoken by plain code: tier 0 ("what should I work on", "what am I waiting on",
"what does Pat owe me", "how many tasks do I have") and the pulse (due today, overdue
follow-ups, and waking up for an event's heads-up). Synthetic tasks in the test store;
calendar and reminders faked."""

import asyncio
import datetime as dt
from typing import Any

import pytest

from scout import index, local_intents, pulse, reminders_mac, tasks
from scout.tools import Timers

TZ = dt.timezone(dt.timedelta(hours=-4))
NOW = dt.datetime(2026, 10, 13, 9, 5, tzinfo=TZ)  # a Tuesday
TODAY = NOW.date()


@pytest.fixture
def store() -> index.Index:
    s = index.default()
    assert isinstance(s, index.Index)
    return s


def add(store: index.Index, title: str, **fields: Any) -> int:
    return store.task_add(title, {k: v for k, v in fields.items() if v is not None})


@pytest.fixture
def some(store: index.Index) -> index.Index:
    add(store, "Send Pat the budget", due="2026-10-13", importance="high")
    add(store, "Review the deck", due="2026-10-09")
    add(store, "Book flights", effort="small")
    add(store, "Vendor quote", owner="Pat Doe", due="2026-10-09")
    add(store, "Slides for the review", owner="Pat", due="2026-10-16")
    add(store, "Signed contract", owner="Sam")
    add(store, "Old thing", status="done", due="2026-10-01")
    add(store, "Renew the lease", due="2026-10-20", **{"from": "mail"})
    return store


def ctx() -> local_intents.Context:
    async def people() -> list[str]:
        return []

    return local_intents.Context(Timers(lambda label: None), now=lambda: NOW, people=people)


def ask(text: str) -> tuple[str | None, bool]:
    c = ctx()
    said = asyncio.run(local_intents.answer(text, c))
    return said, c.private


# --- tier 0 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "What should I work on?",
        "what should I work on next",
        "What should I do next?",
        "What's next?",
        "what is next on my list",
        "What are my top priorities",
        "Can you tell me what I should work on next",
    ],
)
def test_what_should_i_work_on(some: index.Index, text: str) -> None:
    if text.startswith("Can you tell me"):
        assert ask(text)[0] is None  # not a whole-request match: Claude
        return
    said, private = ask(text)
    assert said == (
        "Start with Send Pat the budget, due today. Then Review the deck, overdue since October 9. "
        "After that, follow up with Pat Doe on Vendor quote, overdue since October 9."
    )
    assert not private


def test_empty_list(store: index.Index) -> None:
    assert ask("what should I work on")[0] == "Your task list is empty."
    assert ask("what am I waiting on")[0] == "You're not waiting on anyone."
    assert ask("how many tasks do I have")[0] == "You have no open tasks."


@pytest.mark.parametrize(
    "text", ["What's on my list?", "what's on my task list", "What are my tasks", "list my tasks"]
)
def test_whats_on_my_list(some: index.Index, text: str) -> None:
    said, private = ask(text)
    assert said == (
        "You have four tasks, one overdue: Send Pat the budget, Review the deck, Renew the lease, "
        "and one more. You're waiting on three things from others."
    )
    assert private  # a title from an email is spoken: withheld from Claude in strict mode


def test_how_many(some: index.Index) -> None:
    said, private = ask("How many tasks do I have?")
    assert (
        said
        == "You have four open tasks, one overdue, one due today, and you're waiting on three from others."
    )
    assert not private


@pytest.mark.parametrize("text", ["What am I waiting on?", "what am I waiting for", "who owes me anything"])
def test_waiting_on_grouped_by_person(some: index.Index, text: str) -> None:
    said, private = ask(text)
    assert said == (
        "Pat Doe owes you two: Vendor quote, overdue since October 9, and Slides for the review, "
        "due Friday. Sam owes you Signed contract."
    )
    assert not private


@pytest.mark.parametrize(
    "text",
    [
        "What is Pat working on for me?",
        "what's Pat doing for me",
        "What does Pat owe me",
        "what am I waiting on from Pat",
    ],
)
def test_waiting_on_one_person(some: index.Index, text: str) -> None:
    assert ask(text)[0] == (
        "Pat Doe owes you two: Vendor quote, overdue since October 9, and Slides for the review, due Friday."
    )


def test_waiting_on_someone_not_on_the_list_goes_to_claude(some: index.Index) -> None:
    assert ask("what does the bank owe me")[0] is None
    assert ask("what is Alex working on for me")[0] is None
    assert ask("What does Sam owe me?")[0] == "Sam owes you Signed contract."


@pytest.mark.parametrize(
    "text",
    [
        "what should I make for dinner",
        "what should I do",
        "what should I do about the leak",
        "what should I work on first, the deck or the budget",
        "what should I watch next",
        "what's next on my calendar",
        "what's next week look like",
        "what's on my grocery list",
        "what is Pat working on",
        "how many tasks are in the sprint",
        "what am I waiting for the bus",
        "how much does Pat owe me",
    ],
)
def test_close_phrasings_are_not_taken(some: index.Index, text: str) -> None:
    c = ctx()

    async def reminders(*args: Any) -> dict[str, Any]:
        return {"reminders": [{"title": "Milk"}]}

    c.reminders = reminders
    said = asyncio.run(local_intents.answer(text, c))
    if text == "what's on my grocery list":
        assert said == "Your grocery list has Milk."  # still the reminder list
    else:
        assert said is None


def test_spoken_title_is_cut_at_a_word() -> None:
    t = tasks.Task(1, "word " * 40, "", {})
    s = tasks.spoken_title(t)
    assert len(s) <= 90 and s.endswith("word")


# --- pulse ----------------------------------------------------------------------------------


def make(events: list[dict[str, Any]] | None = None) -> pulse.Pulse:
    async def e(start: str, end: str) -> dict[str, Any]:
        return {"events": events or []}

    async def r() -> dict[str, Any]:
        return {"reminders": []}

    async def tk() -> list[tasks.Task]:
        return tasks.all_tasks()  # the test store, read in this thread

    return pulse.Pulse(10, 15, e, r, tk)


def test_pulse_says_due_today_and_follow_ups_once_a_day(some: index.Index) -> None:
    p = make()
    assert asyncio.run(p.check(NOW)) == [
        "Due today: Send Pat the budget.",
        "Pat Doe owes you Vendor quote, overdue since October 9; maybe follow up.",
    ]
    assert asyncio.run(p.check(NOW + dt.timedelta(minutes=15))) == []
    assert asyncio.run(p.check(NOW + dt.timedelta(hours=6))) == []
    tomorrow = asyncio.run(p.check(NOW + dt.timedelta(days=1)))
    assert tomorrow == ["Pat Doe owes you Vendor quote, overdue since October 9; maybe follow up."]


def test_pulse_tasks_wait_for_quiet_hours_to_end(some: index.Index, monkeypatch: pytest.MonkeyPatch) -> None:
    said: list[str] = []
    clock = [dt.datetime(2026, 10, 13, 6, 0, tzinfo=TZ)]
    slept: list[float] = []

    async def say(text: str) -> None:
        said.append(text)

    real = asyncio.sleep

    async def sleep(s: float) -> None:  # the pulse's sleep moves the clock on
        assert said == [] or clock[0].hour >= 8  # nothing in quiet hours
        slept.append(s)
        clock[0] += dt.timedelta(seconds=s)
        await real(0)

    async def go() -> None:
        monkeypatch.setattr(asyncio, "sleep", sleep)
        task = asyncio.create_task(pulse.run(say, make(), "22:00-08:00", now=lambda: clock[0]))
        for _ in range(50):
            if said:
                break
            await real(0)
        task.cancel()
        monkeypatch.undo()

    asyncio.run(go())
    assert said[:1] == ["Due today: Send Pat the budget."]
    assert slept[:8] == [900.0] * 8  # six to eight: every 15 minutes, the last landing on 8:00


def test_pulse_wakes_for_a_heads_up() -> None:
    start = NOW + dt.timedelta(minutes=40)
    ev = {
        "title": "Design review",
        "start": start.isoformat(),
        "end": (start + dt.timedelta(hours=1)).isoformat(),
    }
    p = make([ev])
    assert asyncio.run(p.check(NOW)) == []
    assert p.wait(NOW) == 15 * 60  # the heads-up is 30 minutes off: the usual interval
    t = NOW + dt.timedelta(minutes=15)
    assert asyncio.run(p.check(t)) == []
    assert p.wait(t) == 15 * 60  # exactly at the next interval
    t += dt.timedelta(minutes=15)
    assert asyncio.run(p.check(t)) == ["Heads up: Design review in 10 minutes."]
    assert p.wait(t) == 15 * 60  # said: nothing more to wake for


def test_pulse_wakes_early_for_a_heads_up_between_checks() -> None:
    start = NOW + dt.timedelta(minutes=17)
    ev = {
        "title": "Standup",
        "start": start.isoformat(),
        "end": (start + dt.timedelta(minutes=15)).isoformat(),
    }
    p = make([ev])
    assert asyncio.run(p.check(NOW)) == []
    assert p.wait(NOW) == 7 * 60  # 17 minutes away, a 10 minute lead: wake in 7
    assert asyncio.run(p.check(NOW + dt.timedelta(minutes=7))) == ["Heads up: Standup in 10 minutes."]


def test_quiet_hours_wake_at_their_end() -> None:
    assert pulse._quiet_left("22:00-08:00", dt.datetime(2026, 10, 13, 7, 50, tzinfo=TZ)) == 600
    assert pulse._quiet_left("22:00-08:00", dt.datetime(2026, 10, 13, 23, 0, tzinfo=TZ)) == 9 * 3600


# --- Slack-found tasks are private like mail ones ---------------------------------------------


def test_slack_task_is_shown_like_a_mail_one() -> None:
    ctx_text = "From the Slack conversation #ops: can you send the runbook by Friday?"
    for src, label in (("mail", "a task from an email"), ("slack", "a task from a Slack message")):
        fields = {"from": src}
        assert tasks.found_from(fields) == src
        assert tasks.shown(fields, "Send the runbook", ctx_text, "strict") == (label, "")
        assert tasks.shown(fields, "Send the runbook", ctx_text, "balanced") == ("Send the runbook", "")
        assert tasks.shown(fields, "Send the runbook", ctx_text, "open") == ("Send the runbook", ctx_text)
    # A Slack quote merged into a task you added is withheld outside open mode too.
    mine = "My own note.\n\n" + ctx_text
    assert tasks.shown({}, "Send the runbook", mine, "balanced") == ("Send the runbook", "My own note.")


def test_slack_from_is_kept_when_added(store: index.Index, monkeypatch: pytest.MonkeyPatch) -> None:
    async def away(*a: Any, **kw: Any) -> bool:
        return False

    monkeypatch.setattr(reminders_mac, "available", away)  # no mirroring
    got = asyncio.run(tasks.add({"title": "Send the runbook", "from": "slack"}, store))
    h = store.get(f"task:{got['n']}")
    assert h is not None and tasks.found_from(tasks.of(h).fields) == "slack"
