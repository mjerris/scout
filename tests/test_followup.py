"""Follow-ups for local recall (followup.py): "what did he say about it?" is rewritten into
a standalone question from what was just said, before the store is searched. Detection in
plain code (ordinary questions pay nothing), the refs-only rewrite, the model's rewrite and
its checks and fallbacks, and how tier 1 and recall use it. The model is a fake; the bench
is scripts/bench_followup.py. All data is synthetic."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from typing import Any

import pytest

from scout import followup, index, recall, refs, tier1

NOW = dt.datetime(2026, 10, 8, 15, 30, tzinfo=dt.timezone(dt.timedelta(hours=-4)))


# --- detecting a follow-up -------------------------------------------------------------------

FOLLOWUPS = [
    "what did he say about it?",
    "and when is that due?",
    "what about the other one?",
    "what about Sam?",
    "how about the rear",
    "who was at that meeting",
    "how much will it cost?",
    "what did she say about hiring",
    "and what's the travel cap?",
    "who was that from",
    "what model is it",
    "when is his birthday",
]

ORDINARY = [
    "when did I last change the generator oil",
    "what did Pat say about the Q4 budget",
    "what's my wifi password",
    "what was decided in the vendor meeting",
    "who is the furnace technician",
    "what's on my calendar this week",
    "do I have anything this afternoon",
    "is it going to rain tomorrow",
    "is it going to be windy this afternoon",
    "will it snow tomorrow",
    "how cold will it get tonight",
    "what time is it",
    "what's the weather like",
    "anything from Sam",
    "what was the code that Pat gave me",
    "what's left on my to-do list",
    "when is Pat's birthday",
    "what did Jordan say about the production database and the staging one and it all",
]


@pytest.mark.parametrize("text", FOLLOWUPS)
def test_follow_ups_are_detected(text: str) -> None:
    assert followup.is_followup(text)


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_questions_are_not(text: str) -> None:
    assert not followup.is_followup(text)


# --- the conversation, and the refs-only rewrite ---------------------------------------------


def _hit(id: str, kind: str, title: str, who: str = "", body: str = "") -> index.Hit:
    return index.Hit(id, kind, title, body or title, who, NOW.timestamp() - 86400, {})


class Store:
    """Records by id; counts reads, or raises when a test says nothing may be read."""

    def __init__(self, *hits: index.Hit, forbid: bool = False) -> None:
        self.hits = {h.id: h for h in hits}
        self.forbid = forbid
        self.reads = 0

    def get(self, id: str) -> index.Hit | None:
        if self.forbid:
            raise AssertionError("an ordinary question must not read the store")
        self.reads += 1
        return self.hits.get(id)


BUDGET = _hit("mail:1", "mail", "Q4 budget", "Pat Rivera <pat@example.com>")
FURNACE = _hit("mail:2", "mail", "Furnace service appointment", "Comfort Heating <service@heat.example>")
FLIGHT = _hit("mail:3", "mail", "Your flight confirmation", "Delta <noreply@delta.example>")
HIRING = _hit("meeting:2", "meeting", "1:1 with Pat", "Pat, Mike")


def _log(*said: tuple[str, str, tuple[str, ...]], age: float = 60.0) -> refs.Log:
    log = refs.Log()
    for asked, text, ids in said:
        log.add(refs.Said(time.time() - age, "Scout", text, ids, False, "room", asked))
    return log


def _turns(log: refs.Log, store: Store) -> list[followup.Turn]:
    return followup.recent(log, store)


def test_recent_turns_carry_what_was_asked_said_and_the_records() -> None:
    store = Store(BUDGET)
    log = _log(("what did Pat say about the Q4 budget", "Pat said it's approved at 48,000.", ("mail:1",)))
    (t,) = _turns(log, store)
    assert t.asked == "what did Pat say about the Q4 budget"
    assert t.said == "Pat said it's approved at 48,000."
    assert t.records == [("mail", "Q4 budget", "Pat Rivera")]


def test_turns_older_than_ten_minutes_are_forgotten() -> None:
    log = _log(("what did Pat say about the Q4 budget", "Pat said 48,000.", ("mail:1",)), age=700)
    assert _turns(log, Store(BUDGET)) == []


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("what did she say about hiring", "what did Pat say about hiring"),
        ("and when is her birthday", "when is Pat's birthday"),
        ("how much will it cost?", "how much will the Q4 budget email cost?"),
        ("who was that from", "who was the Q4 budget email from"),
    ],
)
def test_one_record_and_one_person_resolve_without_the_model(text: str, want: str) -> None:
    store = Store(BUDGET)
    log = _log(("what did Pat say about the Q4 budget", "Pat said it's approved at 48,000.", ("mail:1",)))
    assert followup.resolve(text, _turns(log, store)) == want


def test_the_refs_alone_dont_resolve_what_is_ambiguous() -> None:
    two = Store(BUDGET, FLIGHT)
    log = _log(
        ("anything from Pat or Delta", "Pat on the budget; Delta on your flight.", ("mail:1", "mail:3"))
    )
    assert followup.resolve("how much will it cost", _turns(log, two)) is None  # which one?
    # A sender nobody mentioned isn't "he": the technician named in the answer may be.
    store = Store(FURNACE)
    log = _log(("who is the furnace technician", "Your technician is Luis.", ("mail:2",)))
    assert followup.resolve("and when is he coming", _turns(log, store)) is None
    # Two people in the conversation: which "he"?
    store = Store(HIRING)
    log = _log(("what did Pat and Mike talk about", "Pat and Mike talked about hiring.", ("meeting:2",)))
    assert followup.resolve("what did he say about the roadmap", _turns(log, store)) is None
    # Something left out ("and the rear?") needs the conversation's words, not a title.
    assert followup.resolve("what about the other one", _turns(log, store)) is None
    assert followup.resolve("and what's the travel cap", _turns(log, store)) is None


def test_a_meeting_person_mentioned_in_the_conversation_resolves() -> None:
    store = Store(HIRING)
    log = _log(("when does Pat want the hiring plan", "Pat wants it by October 20.", ("meeting:2",)))
    assert followup.resolve("what is she worried about", _turns(log, store)) == "what is Pat worried about"


# --- the model's rewrite ------------------------------------------------------------------------


class Model:
    """The local model: replies with `out` (or sleeps past the timeout); records prompts."""

    def __init__(self, out: str = "", sleep: float = 0.0) -> None:
        self.ready = True
        self.out, self.sleep = out, sleep
        self.seen: list[tuple[str, str, int, str]] = []

    async def complete(
        self, system: str, user: str, max_tokens: int = 120, timeout_s: float = 20.0, prefix: str = ""
    ) -> str:
        self.seen.append((system, user, max_tokens, prefix))
        if self.sleep:
            await asyncio.sleep(self.sleep)
        return self.out


def _budget_turns() -> list[followup.Turn]:
    log = _log(("what did Pat say about the Q4 budget", "Pat said it's approved at 48,000.", ("mail:1",)))
    return _turns(log, Store(BUDGET))


def test_a_title_is_said_as_a_thing() -> None:
    flight = Store(FLIGHT)
    log = _log(("what's my Denver flight's code", "The code is HXQ7PL.", ("mail:3",)))
    got = followup.resolve("and when does it leave", _turns(log, flight))
    assert got == "when does the flight confirmation email leave"


def test_the_model_rewrites_with_the_conversation_as_json() -> None:
    m = Model('what is the travel cap in the Q4 budget"}')
    got = asyncio.run(followup.by_model("and what's the travel cap?", _budget_turns(), m))
    assert got == "what is the travel cap in the Q4 budget"
    _, user, max_tokens, prefix = m.seen[0]
    assert prefix == followup.PREFIX and max_tokens <= 48
    assert "what did Pat say about the Q4 budget" in user and "Q4 budget" in user
    assert "and what's the travel cap?" in user


@pytest.mark.parametrize(
    "out",
    [
        "not json at all",
        '"}',  # empty
        'what did he say about it"}',  # an echo names nothing new
        '{"query": "what did Pat say about the Q4 budget"}',  # not the asked-for key
        'what did Pat say about the roof repair"}',  # "roof repair" was never said: invented
        "x" * 400 + '"}',
    ],
)
def test_invalid_or_invented_rewrites_fall_back(out: str) -> None:
    assert asyncio.run(followup.by_model("what did he say about it", _budget_turns(), Model(out))) is None


def test_a_slow_rewrite_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(followup, "TIMEOUT_S", 0.05)
    m = Model('what did Pat say about the Q4 budget"}', sleep=1.0)
    assert asyncio.run(followup.by_model("what did he say about it", _budget_turns(), m)) is None


def test_a_rewrite_only_uses_words_from_the_conversation() -> None:
    turns = _budget_turns()
    assert followup.grounded("and the travel cap?", "the travel cap in the Q4 budget from Pat", turns)
    assert not followup.grounded("and the travel cap?", "the travel cap for the Denver trip", turns)


# --- standalone: the whole step ------------------------------------------------------------------


def test_an_ordinary_question_reads_nothing_and_asks_no_model() -> None:
    m = Model('x"}')
    log = _log(("what did Pat say about the Q4 budget", "Pat said 48,000.", ("mail:1",)))
    got = asyncio.run(
        followup.standalone("when did I last change the generator oil", log, Store(forbid=True), m)
    )
    assert got is None and m.seen == []


def test_a_follow_up_with_nothing_said_lately_stays_as_asked() -> None:
    m = Model('x"}')
    assert asyncio.run(followup.standalone("what did he say about it", refs.Log(), Store(), m)) is None
    assert m.seen == []


def test_standalone_uses_the_refs_first_then_the_model() -> None:
    log = _log(("what did Pat say about the Q4 budget", "Pat said it's approved at 48,000.", ("mail:1",)))
    m = Model('what is the travel cap in the Q4 budget"}')
    got = asyncio.run(followup.standalone("what did she say about hiring", log, Store(BUDGET), m))
    assert got == followup.Rewrite("what did Pat say about hiring", "Q4 budget hiring", "refs", ["mail:1"])
    assert m.seen == []
    got = asyncio.run(followup.standalone("and what's the travel cap?", log, Store(BUDGET), m))
    want = followup.Rewrite(
        "what is the travel cap in the Q4 budget", "Q4 budget travel cap", "model", ["mail:1"]
    )
    assert got == want
    assert len(m.seen) == 1


def test_a_store_that_is_down_leaves_the_question_as_asked() -> None:
    class Down:
        def get(self, id: str) -> index.Hit | None:
            raise OSError("memory service down")

    log = _log(("what did Pat say about the Q4 budget", "Pat said 48,000.", ("mail:1",)))
    m = Model('what did Pat say about the Q4 budget"}')
    # The conversation's words are still there, without the records' titles.
    got = asyncio.run(followup.standalone("what did he say about it", log, Down(), m))
    assert got == followup.Rewrite("what did Pat say about the Q4 budget", "", "model")


def test_search_words_are_the_titles_meant_and_the_follow_ups_own_words() -> None:
    log = _log(
        ("anything from Pat or Delta", "Pat on the budget; Delta on your flight.", ("mail:1", "mail:3"))
    )
    turns = _turns(log, Store(BUDGET, FLIGHT))
    q = "what is the confirmation code in the flight confirmation email"
    assert (
        followup.search_words("what's the code in the other one", q, turns) == "Your flight confirmation code"
    )


# --- tier 1: routing the standalone question --------------------------------------------------


class LM(tier1.LocalModel):
    """tier1.LocalModel with a fake generator: the rewrite's reply, then the route."""

    def __init__(self, rewrite_out: str, route_out: str) -> None:
        super().__init__("fake", extra=True)
        self.ready, self.rewrite = True, True
        self.outs = {"rewrite": rewrite_out, "route": route_out}
        self.chats: list[tuple[str, str]] = []

    def _generate(self, chat: list[dict[str, str]], max_tokens: int, prefix: str = "") -> str:
        kind = "rewrite" if chat[0]["content"] == followup.SYSTEM else "route"
        self.chats.append((kind, chat[-1]["content"]))
        return self.outs[kind]


def _lm(rewrite_out: str, route_out: str = '{"route": "recall", "query": "Q4 budget travel cap"}') -> LM:
    lm = LM(rewrite_out, route_out)
    store = index.default()
    assert isinstance(store, index.Index)
    store.put("mail:1", "mail", "Q4 budget", "The Q4 budget is approved at 48,000 dollars. Travel is capped at 6,000.",
              who="Pat Rivera <pat@example.com>", ts=NOW.timestamp() - 3 * 86400)  # fmt: skip
    lm.store = store
    lm.said = _log(("what did Pat say about the Q4 budget", "Pat said it's approved at 48,000.", ("mail:1",)))
    return lm


def test_a_follow_up_about_what_was_just_said_is_a_recall_without_routing() -> None:
    lm = _lm('what is the travel cap in the Q4 budget"}')
    try:
        d = asyncio.run(lm.decide("and what's the travel cap?", NOW))
    finally:
        lm._pool.shutdown()
    assert d == {
        "tool": "recall",
        "args": {
            "query": "Q4 budget travel cap",
            "question": "what is the travel cap in the Q4 budget",
            "asked": "and what's the travel cap?",
            "refs": ["mail:1"],
        },
    }
    assert [k for k, _ in lm.chats] == ["rewrite"]


def test_a_follow_up_about_something_else_is_routed_as_the_standalone_question() -> None:
    # The store is down for the records: the rewrite names no record said, so it's routed.
    lm = _lm('what did Pat say about the Q4 budget"}', '{"route": "recall", "query": "Pat Q4 budget"}')

    class Down:
        def get(self, id: str) -> index.Hit | None:
            raise OSError("down")

        def search(self, *a: Any, **kw: Any) -> list[index.Hit]:
            return []

    lm.store = Down()
    try:
        d = asyncio.run(lm.decide("what did he say about it", NOW))
    finally:
        lm._pool.shutdown()
    assert [k for k, _ in lm.chats] == ["rewrite", "route"]
    assert lm.chats[1][1] == "what did Pat say about the Q4 budget"
    assert d is not None and d["args"]["asked"] == "what did he say about it"


def test_a_follow_up_that_cant_be_written_out_goes_to_claude() -> None:
    # Its own few words would find something else ("what about the rear?": the bike's brakes).
    log = _log(("what did Pat say about the Q4 budget", "Pat said 48,000.", ("mail:1",)))
    got = asyncio.run(followup.standalone("what about the rear?", log, Store(BUDGET), Model("not json")))
    assert got is not None and got.question == ""
    lm = _lm("not json")
    try:
        assert asyncio.run(lm.decide("what about the rear?", NOW)) is None
    finally:
        lm._pool.shutdown()
    assert [k for k, _ in lm.chats] == ["rewrite"]


def test_with_the_rewrite_off_a_follow_up_is_routed_as_asked() -> None:
    lm = _lm('what is the travel cap in the Q4 budget"}')
    lm.rewrite = False
    try:
        d = asyncio.run(lm.decide("what did he say about it", NOW))
    finally:
        lm._pool.shutdown()
    assert [k for k, _ in lm.chats] == ["route"]
    assert d is not None and "asked" not in d["args"]


def test_what_about_doing_something_goes_to_claude_even_as_asked() -> None:
    assert tier1.guard("what about letting Sam know", extra=True, on_record=True)
    assert tier1.guard("what about hiring", extra=True, on_record=True) is None


def test_a_leading_and_is_one_question_only_on_the_follow_up_path() -> None:
    # As asked (the rewrite off), "and when..." is still two things; as a follow-up, one.
    assert tier1.guard("and when is that due", extra=True, on_record=True)
    assert tier1.unsafe("and when is that due", extra=True, follow_up=True) is None
    assert tier1.unsafe("and telling him is fine", extra=True, follow_up=True)


@pytest.mark.parametrize(
    "text",
    [
        "and forward that to him",
        "and send it to Sam",
        "what about letting him know",
        "should we go with them",
    ],
)
def test_a_follow_up_that_acts_or_judges_goes_to_claude_without_a_rewrite(text: str) -> None:
    lm = _lm('what did Pat say about the Q4 budget"}', '{"route": "recall", "query": "budget"}')
    try:
        assert asyncio.run(lm.decide(text, NOW)) is None
    finally:
        lm._pool.shutdown()
    assert lm.chats == []


def test_the_conversations_words_only_ever_make_a_recall() -> None:
    # A title with an action word ("Generator oil change"), or a record that says to do
    # something, is the conversation's, not the user's: the follow-up is still only read.
    lm = _lm('what did Pat say, and send the Q4 budget"}')
    lm.said = _log(("what did Pat say", "Pat said send the Q4 budget.", ("mail:1",)))
    try:
        d = asyncio.run(lm.decide("and what about it", NOW))
    finally:
        lm._pool.shutdown()
    assert d is not None and d["tool"] == "recall" and d["tool"] in tier1.EXTRA_TOOLS


# --- recall: searched by the standalone question, answered to the user's words -----------------


class AnswerModel:
    ready = True

    def __init__(self, out: dict[str, Any]) -> None:
        self.out = json.dumps(out)
        self.seen: list[str] = []

    async def complete(self, system: str, user: str, max_tokens: int) -> str:
        self.seen.append(user)
        return self.out


class SearchStore:
    def __init__(self, *hits: index.Hit) -> None:
        self.hits, self.queries = list(hits), list[str]()

    def search(
        self, query: str, kinds: Any = None, since: Any = None, limit: int = 8, semantic: Any = True
    ) -> list[index.Hit]:
        self.queries.append(query)
        return self.hits


def test_recall_searches_the_standalone_question_and_shows_the_users_words() -> None:
    h = _hit("mail:1", "mail", "Q4 budget", "Pat Rivera <pat@example.com>",
             "The Q4 budget is approved at 48,000 dollars. Travel is capped at 6,000.")  # fmt: skip
    store = SearchStore(h)
    m = AnswerModel(
        {
            "found": True,
            "source": 1,
            "quote": "Travel is capped at 6,000",
            "answer": "Travel is capped at 6,000.",
        }
    )
    got = asyncio.run(
        recall.answer("what is the travel cap in the Q4 budget", "travel cap", m, store, NOW,
                      asked="and what's the travel cap?")
    )  # fmt: skip
    assert got is not None and "6,000" in got.text
    assert "and what's the travel cap?" not in store.queries
    assert "Question: and what's the travel cap?" in m.seen[0]
    assert "what is the travel cap in the Q4 budget" in m.seen[0]


def test_recall_checks_the_answer_against_the_standalone_question() -> None:
    # "what did he say about it" has no topic words of its own: the standalone one's are checked.
    h = _hit(
        "mail:1",
        "mail",
        "Q4 budget",
        "Pat Rivera <pat@example.com>",
        "The Q4 budget is approved at 48,000 dollars.",
    )
    m = AnswerModel(
        {
            "found": True,
            "source": 1,
            "quote": "approved at 48,000 dollars",
            "answer": "Pat said 48,000 dollars.",
        }
    )
    asked = "what did he say about it"
    assert asyncio.run(recall.answer(asked, "", m, SearchStore(h), NOW)) is None  # as asked: no topic
    got = asyncio.run(
        recall.answer("what did Pat say about the Q4 budget", "", m, SearchStore(h), NOW, asked=asked)
    )
    assert got is not None
    m2 = AnswerModel(
        {"found": True, "source": 1, "quote": "approved at 48,000 dollars", "answer": "48,000 dollars."}
    )
    roof = asyncio.run(
        recall.answer("what did Pat say about the roof", "", m2, SearchStore(h), NOW, asked=asked)
    )
    assert roof is None  # the record doesn't cover the roof


def test_a_follow_ups_own_words_must_all_be_in_the_record() -> None:
    denver = _hit("mail:3", "mail", "Your flight confirmation", "Delta <noreply@delta.example>",
                  "Flight DL 1442 from Detroit to Denver departs November 12. Confirmation code HXQ7PL.")  # fmt: skip
    r = {
        "found": True,
        "source": 1,
        "quote": "Confirmation code HXQ7PL",
        "answer": "Confirmation code HXQ7PL.",
    }
    q = "what's the confirmation code for your Chicago flight"  # 1 of 4 topic words missing: covered...
    assert not isinstance(recall.check(q, r, [denver]), str)
    got = recall.check(q, r, [denver], asked="what about my Chicago flight?")  # ...but the follow-up's isn't
    assert isinstance(got, str) and "chica" in got
    other = recall.check(q.replace("Chicago", "Denver"), r, [denver], asked="and the other one?")
    assert not isinstance(other, str)  # "the other one" names no topic


def test_the_records_just_spoken_of_are_read_first() -> None:
    class Store(SearchStore):
        def get(self, id: str) -> index.Hit | None:
            return {"mail:1": BUDGET}.get(id)

    m = AnswerModel({"found": True, "source": 1, "quote": "Q4 budget", "answer": "The Q4 budget, from Pat."})
    got = asyncio.run(
        recall.answer("who was the Q4 budget email from", "Q4 budget", m, Store(), NOW,
                      asked="who was that from", also=["mail:1"])
    )  # fmt: skip
    assert got is not None and got.source.id == "mail:1"
    assert "[1] (an email" in m.seen[0]


def test_tier1_run_passes_the_users_words_to_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    from scout.summary import Summaries

    seen: dict[str, Any] = {}

    async def answer(question: str, query: str, model: Any, store: Any, now: Any, **kw: Any) -> None:
        seen.update(question=question, query=query, **kw)

    monkeypatch.setattr(recall, "answer", answer)
    d = {
        "tool": "recall",
        "args": {"query": "q", "question": "standalone", "asked": "and it?", "refs": ["mail:1"]},
    }
    m = AnswerModel({})
    asyncio.run(tier1.run(d, NOW, Summaries(m), store=SearchStore()))
    assert seen["question"] == "standalone" and seen["asked"] == "and it?" and seen["also"] == ["mail:1"]


def test_the_knob_sets_the_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_routing import make

    made: list[Any] = []

    class Model:
        def __init__(self, name: str, timeout_s: float = 3.0, extra: bool = False) -> None:
            made.append(self)

        async def load(self) -> None:
            pass

    monkeypatch.setattr(tier1, "LocalModel", Model)

    async def go(on: bool) -> Any:
        r: Any = make()
        r.a.cfg.claude.local_rewrite = on
        await r.a.start_tier1()
        return r.a

    a = asyncio.run(go(True))
    assert made[-1].rewrite is True and made[-1].said is a.said
    asyncio.run(go(False))
    assert made[-1].rewrite is False
