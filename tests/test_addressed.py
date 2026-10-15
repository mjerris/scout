"""addressed.reason: is speech without the wake word a follow-up to Scout's answer, or
room conversation? Measured on synthetic sets (README, "Follow-ups without the wake
word"). The dev set chose the rules. The second set was written as held out, measured
once (precision 0.69, recall 0.96), then tuned on, so it is a dev set now too. The
fresh set was written after that; measured once (precision 0.71, recall 1.00), one bug
it showed fixed (a name after a greeting, "Hey Dana, it's me", wasn't seen as said to
Dana): 0.74 and 1.00."""

from __future__ import annotations

import pytest

from scout import addressed

WEATHER = "It's 58 and mostly clear in Luther, wind southeast at 6."
MEETINGS = "You have three meetings tomorrow: standup at 9, design review at 11, and a call with Brian at 2."
OFFER = "Want me to draft a reply to Sam?"
PERSON = "Brian West is five foot nine."
TIMER = "The timer is set for 10 minutes."
MAIL = "Your newest email is from Dana about the Q3 budget."
TRAFFIC = "Traffic to the airport is about 40 minutes."
LIST = "Done, I added milk to the list."
NONE = ""

# (the answer just given, what was heard next): real follow-ups.
DEV_FOLLOW_UPS = [
    (WEATHER, "What about tomorrow?"),
    (WEATHER, "And Saturday?"),
    (WEATHER, "How windy will it be?"),
    (WEATHER, "In Celsius."),
    (WEATHER, "For tomorrow."),
    (WEATHER, "Wait, actually make it Saturday."),
    (WEATHER, "Should I bring a jacket?"),
    (MEETINGS, "Move the one with Brian to 3."),
    (MEETINGS, "Cancel the design review."),
    (MEETINGS, "Who's in the design review?"),
    (MEETINGS, "The second one."),
    (MEETINGS, "Tell Brian I'll be late."),
    (MEETINGS, "Remind me 10 minutes before standup."),
    (OFFER, "Yes please."),
    (OFFER, "Sure, go ahead."),
    (OFFER, "No thanks."),
    (OFFER, "No, send it to Alex instead."),
    (PERSON, "How old is he?"),
    (PERSON, "And his phone number?"),
    (PERSON, "Text him that I'm running late."),
    (TIMER, "Make it 15."),
    (TIMER, "Actually, cancel it."),
    (TIMER, "And set another one for 30 minutes."),
    (MAIL, "Read it to me."),
    (MAIL, "What does it say?"),
    (MAIL, "Reply that I'll review it Friday."),
    (MAIL, "Any others from her?"),
    (TRAFFIC, "How about by train?"),
    (TRAFFIC, "What if I leave at 5?"),
    (LIST, "Add eggs too."),
    (LIST, "And bread."),
    (LIST, "I meant oat milk."),
    (LIST, "What else is on it?"),
    (WEATHER, "Okay, what about Denver?"),
    (MEETINGS, "Say that again."),
    (WEATHER, "What about Friday, Scout?"),
    (MEETINGS, "I'd like the one at noon instead."),
    (MAIL, "Just the first one."),
    (MEETINGS, "Don't move the standup."),
    (MEETINGS, "When does the design review end?"),
]

# Room conversation heard inside the window: a call or a meeting near the mic.
DEV_CONVERSATION = [
    (NONE, "It's asking me my..."),
    (WEATHER, "It's not just memory, by the way, Brian. It's more complicated than that."),
    (WEATHER, "Oh, I know."),
    (PERSON, "Good calls, Brian."),
    (PERSON, "Just don't fall into the trap thinking memory is the only solution to all the problems."),
    (PERSON, "Because that's a road that's very hard to go down."),
    (NONE, "Yeah, this is the wrong account. I'm on the wrong account."),
    (NONE, "Know that I even have access to all these things."),
    (NONE, "You will just authenticate what you can or you have, and that's what it will expose to you."),
    (NONE, "It says, how do I enable?"),
    (NONE, "It is a connect on everything."),
    (NONE, "You will want to be able to, because mine will connect."),
    (NONE, "What that allows you to do when you're searching for something that..."),
    (NONE, "When you're searching for something that people said, it's a certain time."),
    (NONE, "I did."),
    (PERSON, "Thank you, Scott."),
    (MEETINGS, "You might have to ask Brian."),
    (MEETINGS, "No, I think he's in Denver this week."),
    (MEETINGS, "Brian, can you see my screen?"),
    (MEETINGS, "Mike, you're on mute."),
    (NONE, "Hi everyone, sorry I'm late."),
    (NONE, "Hold on, let me share my screen."),
    (NONE, "Sounds good, talk to you tomorrow."),
    (NONE, "Okay, so the next step is the migration."),
    (NONE, "Yeah, exactly."),
    (NONE, "Right, right."),
    (NONE, "That's what I was saying."),
    (NONE, "What do you think, Brian?"),
    (NONE, "Mine will connect too."),
    (NONE, "Because of the firewall."),
    (WEATHER, "Thank you."),
    (NONE, "We shipped that last week."),
    (NONE, "They want it by the end of the quarter."),
    (NONE, "Honestly the whole thing needs a rewrite."),
    (NONE, "The demo went really well yesterday."),
]

SECOND_FOLLOW_UPS = [
    (WEATHER, "Is it going to rain this weekend?"),
    (WEATHER, "What time does the rain start?"),
    (MEETINGS, "And the day after?"),
    (MEETINGS, "What's the call with Brian about?"),
    (OFFER, "Yes, and say I'll call tomorrow."),
    (PERSON, "What's his email?"),
    (TIMER, "Actually make it 20 minutes."),
    (MAIL, "Archive it."),
    (MAIL, "Summarize it."),
    (MAIL, "Who else is on it?"),
    (TRAFFIC, "Send me the route."),
    (TRAFFIC, "And back?"),
    (LIST, "Remove the milk."),
    (LIST, "Can you read me the whole list?"),
    (MEETINGS, "The earlier one."),
    (MEETINGS, "Saturday instead."),
    (WEATHER, "Hmm, and Sunday?"),
    (MEETINGS, "Repeat that."),
    (MAIL, "Could you spell her last name?"),
    (MEETINGS, "What was the first one?"),
    (OFFER, "Sure."),
    (TIMER, "How much time is left?"),
    (LIST, "Put cereal on it too."),
    (MAIL, "Forward it to Alex."),
    (TRAFFIC, "I need to be there by six."),
]

SECOND_CONVERSATION = [
    (NONE, "Can you hear me okay?"),
    (NONE, "Did you get my email?"),
    (NONE, "So what's the plan for Thursday?"),
    (NONE, "Next slide please."),
    (NONE, "Go back one."),
    (NONE, "Interesting, tell me more."),
    (NONE, "This side first."),
    (NONE, "Slack workspace? What is it?"),
    (NONE, "Is it signalwire.com? Signalwire, uh-huh. Yes."),
    (NONE, "Why is this so terrible?"),
    (MEETINGS, "Sarah, do you want to take this one?"),
    (NONE, "I don't know, maybe after lunch."),
    (NONE, "He said he'd send it over tonight."),
    (NONE, "You should talk to the infra team about it."),
    (NONE, "There's a bug in the login flow."),
    (NONE, "Uh-huh."),
    (NONE, "Totally."),
    (NONE, "Okay, okay, I see what you mean."),
    (NONE, "My connection keeps dropping, sorry."),
    (NONE, "Let me pull up the doc real quick."),
    (WEATHER, "It's cold out there today."),
    (NONE, "We should probably wrap up, it's almost six."),
    (NONE, "If that works for everyone, let's do Tuesday."),
    (NONE, "Anyway, where was I..."),
    (NONE, "Thanks, Jen, that's helpful."),
]


RECIPE = "Preheat the oven to 375 and bake for 25 minutes."
CALL = "Okay, calling Dana on her mobile."
SLACK = "Priya asked in the release channel whether the build is green."
STOCK = "Apple closed at 212, up 1.4 percent."

FRESH_FOLLOW_UPS = [
    (RECIPE, "How long should it rest after?"),
    (RECIPE, "What temperature in Celsius?"),
    (RECIPE, "Set a timer for 25 minutes."),
    (RECIPE, "And if I double it?"),
    (CALL, "Actually, call her work number."),
    (CALL, "Hang up."),
    (SLACK, "Tell her it's green."),
    (SLACK, "Who else replied?"),
    (SLACK, "Read me the thread."),
    (SLACK, "What did she say before that?"),
    (STOCK, "What about Microsoft?"),
    (STOCK, "And Google?"),
    (STOCK, "How did it do this week?"),
    (OFFER, "Yes, do it."),
    (OFFER, "Not now."),
    (MEETINGS, "Push standup to 9:30."),
    (MEETINGS, "Which room is the design review in?"),
    (MEETINGS, "Add Priya to the call with Brian."),
    (WEATHER, "Tomorrow morning?"),
    (WEATHER, "Will it be windy at the lake?"),
    (MAIL, "Mark it unread."),
    (MAIL, "When did she send it?"),
    (LIST, "Take the milk off."),
    (TIMER, "Pause it."),
    (TRAFFIC, "Is there a faster way?"),
]

FRESH_CONVERSATION = [
    (RECIPE, "Smells amazing in here."),
    (RECIPE, "I'll set the table."),
    (CALL, "Hey Dana, it's me."),
    (CALL, "Hi, is this a good time?"),
    (SLACK, "Priya, is the build green on your side?"),
    (SLACK, "I think it's still running."),
    (STOCK, "That's a good day for them."),
    (STOCK, "My portfolio is mostly index funds."),
    (MEETINGS, "Can everyone see the slides?"),
    (MEETINGS, "Let's take five minutes."),
    (MEETINGS, "Does that make sense?"),
    (MEETINGS, "Are you still there?"),
    (MEETINGS, "I'll send the notes after."),
    (WEATHER, "It was so hot last summer."),
    (WEATHER, "Kids, grab your coats."),
    (MAIL, "Dana always sends these late."),
    (MAIL, "You know what I mean?"),
    (LIST, "We're out of coffee too, by the way."),
    (TIMER, "Don't forget the garlic bread."),
    (TRAFFIC, "Ugh, rush hour."),
    (TRAFFIC, "We'll take the back roads."),
    (NONE, "Wait, who's presenting next?"),
    (NONE, "Could you repeat the question?"),
    (NONE, "Mute yourself, there's an echo."),
    (NONE, "Sorry, go ahead."),
]


def _score(pos: list[tuple[str, str]], neg: list[tuple[str, str]]) -> tuple[float, float, list[str]]:
    """Precision and recall of taking speech as a follow-up; and the misses."""
    accepted = [(a, t) for a, t in pos if addressed.reason(t, a, ["scout"], ["scott"]) is None]
    false = [(a, t) for a, t in neg if addressed.reason(t, a, ["scout"], ["scott"]) is None]
    tp = len(accepted)
    precision = tp / max(tp + len(false), 1)
    recall = tp / max(len(pos), 1)
    misses = [f"missed: {t}" for a, t in pos if (a, t) not in accepted] + [f"took: {t}" for _, t in false]
    return precision, recall, misses


def test_dev_set_precision_and_recall() -> None:
    precision, recall, misses = _score(DEV_FOLLOW_UPS, DEV_CONVERSATION)
    print(f"dev: precision {precision:.2f} recall {recall:.2f}; {misses}")
    assert precision == 1.0 and recall == 1.0, misses


def test_second_set_precision_and_recall() -> None:
    precision, recall, misses = _score(SECOND_FOLLOW_UPS, SECOND_CONVERSATION)
    print(f"second: precision {precision:.2f} recall {recall:.2f}; {misses}")
    assert precision >= 0.75 and recall >= 0.95, misses


def test_fresh_set_precision_and_recall() -> None:
    precision, recall, misses = _score(FRESH_FOLLOW_UPS, FRESH_CONVERSATION)
    print(f"fresh: precision {precision:.2f} recall {recall:.2f}; {misses}")
    assert precision >= 0.7 and recall >= 0.85, misses


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("It's asking me my...", "unfinished"),
        ("It's not just memory, by the way, Brian.", "said to Brian"),
        ("Oh, I know.", "acknowledgement"),
        ("We shipped that last week.", "statement"),
    ],
)
def test_the_reason_says_why(text: str, why: str) -> None:
    reason = addressed.reason(text, WEATHER)
    assert reason is not None and why in reason


def test_naming_scout_anywhere_counts() -> None:
    assert addressed.reason("We shipped that last week, right Scout?", NONE) is None
