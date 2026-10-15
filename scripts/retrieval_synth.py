"""Synthetic data for scripts/eval_retrieval.py (nothing here is real).

- corpus(): a year of ~20,000 short Scout turns in the needles' topics (RV upkeep,
  travel, family, health, errands), some deliberately close to them; with NEEDLES, the
  50 hand-written questions behind index.py's "top 5 for 78-82%" (kept as measured:
  same seed, same order).
- long_docs() and LONG_NEEDLES: meeting summaries in markdown, a heading per section
  and several thousand characters each, every project with the same sections, and
  questions that need one section of one meeting (what section-aware windows are for).
"""

# ruff: noqa: S311, E501, E731  (synthetic filler, not security; data tables)
from __future__ import annotations

import random
from collections.abc import Callable


R = random.Random(7)
PEOPLE = ["Pat", "Sam", "Alex", "Jordan", "Chris", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Lee", "Dana"]
TOWNS = ["Luther", "Cadillac", "Baldwin", "Manistee", "Ludington", "Traverse City", "Grand Rapids", "Big Rapids", "Reed City", "Lake City", "Tustin", "Marion"]
PARTS = ["front tires", "awning", "slide-out", "water heater", "generator", "inverter", "converter", "roof", "leveling jacks", "step", "entry door", "windshield wipers", "headlights", "fresh tank", "gray tank", "air conditioner", "furnace", "microwave", "TV antenna", "batteries", "solar panels", "hitch", "mud flaps", "mirrors", "toilet", "shower drain", "kitchen faucet", "cabinet latch", "bunk light", "smoke alarm"]
ISSUES = ["is making a noise", "needs cleaning", "looks worn", "is leaking a little", "stopped working", "is fine now", "needs a new part", "was serviced", "needs checking before the trip", "is loose"]
FOODS = ["tacos", "pancakes", "chili", "burgers", "soup", "pasta", "salad", "stir fry", "fish", "pizza"]
WEATHER = ["rain", "snow", "wind", "sun", "fog", "storms", "frost", "heat"]
APPS = ["Safari", "Mail", "Music", "Maps", "Calendar", "Notes"]

def num(a: int, b: int) -> int:
    return R.randint(a, b)

T: list[Callable[[], tuple[str, str]]] = [
    lambda: (f"What's the weather in {R.choice(TOWNS)}", f"{R.choice(WEATHER).capitalize()} today, high of {num(30,85)}, low of {num(10,60)}."),
    lambda: (f"Will it {R.choice(['rain','snow','storm'])} tomorrow in {R.choice(TOWNS)}", f"{num(10,90)} percent chance, mostly in the {R.choice(['morning','afternoon','evening'])}."),
    lambda: (f"Set a timer for {num(2,45)} minutes", "Timer set."),
    lambda: (f"What's on my calendar {R.choice(['today','tomorrow','Friday','this weekend'])}", f"You have {R.choice(['nothing','a call with '+R.choice(PEOPLE),'lunch with '+R.choice(PEOPLE),'the '+R.choice(PARTS)+' appointment'])} at {num(8,17)}:{R.choice(['00','30'])}."),
    lambda: (f"Any new mail from {R.choice(PEOPLE)}", f"{num(0,3)} new messages from {R.choice(PEOPLE)}, about {R.choice(['the trip','dinner','a package','photos','the bill'])}."),
    lambda: (f"Note that the {R.choice(PARTS)} {R.choice(ISSUES)}", "Noted."),
    lambda: (f"The {R.choice(PARTS)} {R.choice(ISSUES)}, remember that", "Okay, I'll remember."),
    lambda: (f"How far is {R.choice(TOWNS)} from {R.choice(TOWNS)}", f"About {num(10,120)} miles, {num(15,140)} minutes."),
    lambda: (f"Find diesel near {R.choice(TOWNS)}", f"There's a {R.choice(['Speedway','Marathon','Shell','Mobil'])} {num(1,15)} miles away."),
    lambda: (f"What should we make for dinner", f"How about {R.choice(FOODS)}?"),
    lambda: (f"Add {R.choice(['milk','eggs','bread','coffee','dog food','paper towels','batteries','rice'])} to the shopping list", "Added."),
    lambda: (f"Open {R.choice(APPS)}", "Opening it."),
    lambda: (f"Remind me to call {R.choice(PEOPLE)} at {num(1,9)}", "Reminder added."),
    lambda: (f"What's the Starlink status", f"Connected, {num(40,250)} megabits down, {num(10,60)} ms latency."),
    lambda: (f"How much data have we used this month", f"{num(50,900)} gigabytes so far."),
    lambda: (f"What time is it in {R.choice(['Arizona','London','Denver','Tokyo'])}", f"It's {num(1,12)}:{num(10,59)}."),
    lambda: (f"What's the tire pressure on the {R.choice(['front tires','truck','tow car','spare'])}", f"{num(30,110)} psi cold."),
    lambda: (f"Who is {R.choice(PEOPLE)}'s {R.choice(['wife','husband','son','daughter','dog'])}", f"That's {R.choice(['Ann','Ben','Cleo','Dex','Eve','Finn'])}."),
    lambda: (f"How much did the {R.choice(PARTS)} repair cost", f"{num(40,900)} dollars."),
    lambda: (f"When is {R.choice(PEOPLE)}'s birthday", f"{R.choice(['January','March','May','July','September','December'])} {num(1,28)}."),
    lambda: (f"What's the campground checkout time in {R.choice(TOWNS)}", f"{num(10,12)} AM."),
    lambda: (f"Play some {R.choice(['jazz','country','classical','rock'])}", "Playing it."),
    lambda: (f"How many steps did I walk", f"{num(1000,14000)} steps today."),
    lambda: (f"Is the {R.choice(PARTS)} under warranty", R.choice(["Yes, until next spring.", "No, it ran out last year.", "I don't know; check the folder."])),
]

def corpus(n: int = 20000, days: int = 365) -> list[tuple[str, str, float]]:
    """(request, reply, age_days), oldest first (the same every call)."""
    R.seed(7)
    out = [(*R.choice(T)(), R.uniform(0, days)) for _ in range(n)]
    out.sort(key=lambda r: -r[2])
    return out


# Hand-written needles: (request, reply, query, kind_of_match, age_days). kind_of_match:
# lex = shares key words; para = synonyms or rephrased; concept = no shared content words.

NEEDLES: list[tuple[str, str, str, str, int]] = [
    ("Remind me what tire pressure the rear duallies need", "The rear duals on the RV run at 80 psi cold.", "what psi for the back tires", "para", 40),
    ("What's my dentist's name again", "Your dentist is Dr. Okafor in Traverse City.", "who cleans my teeth", "concept", 200),
    ("When is the smoke detector battery due", "The smoke detector battery is due in March.", "smoke detector battery", "lex", 90),
    ("Where did I leave the spare key for the truck", "You said the spare truck key is in the blue tackle box.", "where's the extra key for the pickup", "para", 15),
    ("How much did the generator service cost", "The Onan service was 412 dollars at Cummins.", "what did we pay to have the onan looked at", "para", 120),
    ("Add a note that the water pump makes a clicking noise", "Noted: the water pump clicks when it cycles.", "the pump that ticks", "para", 60),
    ("What did the vet say about Biscuit's leg", "The vet said Biscuit sprained her left hind leg and needs two weeks of rest.", "the dog's injury", "concept", 33),
    ("What's the wifi password at the Luther campground", "It's on the office board: Pinecone2026.", "internet login at the campsite in luther", "para", 300),
    ("Who's the mechanic in Cadillac I liked", "Dave at North Country Diesel in Cadillac.", "good repair shop near cadillac", "para", 250),
    ("Remember that the slide-out seal is torn on the left side", "Okay: the left slide-out seal is torn.", "rubber gasket damage on the slide", "para", 75),
    ("What's Mom's flight number for Thanksgiving", "Delta 1442, landing in Grand Rapids at 3:15.", "when does my mother's plane get in", "concept", 25),
    ("How many miles until the next oil change", "About 1,800 miles: the next oil change is at 61,000.", "oil change mileage", "lex", 10),
    ("What size is the furnace filter", "The furnace takes a 14 by 20 by 1 filter.", "heater air filter dimensions", "para", 180),
    ("Did I renew the RV registration", "Yes, you renewed the registration online on the 3rd.", "plates and tags paperwork for the camper", "concept", 140),
    ("What was the name of the pizza place in Ludington", "Old Hamlin was the diner; the pizza was House of Flavors? No: it was Luciano's.", "italian food in ludington", "para", 220),
    ("Note that the black tank sensor reads full when empty", "Noted: the black tank sensor is stuck on full.", "sewage tank gauge is wrong", "para", 50),
    ("What's Jake's birthday", "Jake's birthday is June 14th.", "when was my nephew born", "concept", 280),
    ("How much diesel did we burn last month", "About 140 gallons in September.", "fuel usage in september", "para", 30),
    ("What's the torque spec for the lug nuts", "The lug nuts on the RV are 450 foot-pounds.", "how tight do the wheel bolts go", "para", 160),
    ("Who did I talk to at the insurance company", "You spoke with Maria at Progressive, claim 88231.", "the agent handling my claim", "concept", 95),
    ("What did the doctor say about my blood pressure", "Dr. Chen said 132 over 85, recheck in three months.", "my BP reading", "para", 45),
    ("Remember the solar controller is set to lithium mode", "Okay, the solar charge controller is on the lithium profile.", "battery chemistry setting for the panels", "concept", 110),
    ("Where's the shutoff valve for the fresh water", "Behind the access panel under the kitchen sink.", "how do I turn off the water supply", "para", 260),
    ("What's the gate code at the storage lot", "The storage gate code is 4417.", "code to get into storage", "lex", 70),
    ("What did I order from Amazon for the awning", "You ordered a Carefree awning arm replacement, arriving Thursday.", "the part for the shade cover", "concept", 20),
    ("When does the Starlink bill come out", "Starlink charges on the 9th each month, 120 dollars.", "satellite internet payment date", "para", 85),
    ("What's the name of the guy who fixed the slide motor", "That was Tony from Mobile RV Fix.", "slide motor repair guy", "lex", 190),
    ("Remind me which fuse is for the water heater", "Fuse 7, 15 amps, in the panel by the door.", "breaker for hot water", "para", 130),
    ("What's the max weight the hitch can take", "The hitch is rated for 10,000 pounds, 1,000 tongue weight.", "towing capacity of the receiver", "para", 240),
    ("Which campground had the bad sewer hookup", "Pine Haven in Baldwin: the sewer connection leaked.", "where did the dump connection leak", "para", 310),
    ("What did Sarah say about coming to visit", "Sarah said she'll come the weekend of the 18th.", "my sister's visit", "concept", 12),
    ("How old is the house battery bank", "The lithium bank was installed in May 2024.", "when were the batteries put in", "para", 170),
    ("Note that the bedroom vent fan doesn't close all the way", "Noted: the bedroom roof fan won't fully close.", "roof vent stuck open", "para", 65),
    ("What's the part number for the water filter", "The inline filter is a Camco 40043.", "camco filter number", "lex", 150),
    ("What time is the dump station open in Manistee", "The Manistee dump station is open 8 to 6.", "hours for emptying tanks in manistee", "para", 210),
    ("Remember that I prefer the window seat when flying", "Okay: you like the window seat.", "my airplane seating preference", "para", 330),
    ("What did the electrician say about the converter", "He said the converter is fine; the problem was a loose ground.", "why the power was acting up", "concept", 100),
    ("Where's the nearest Costco to Traverse City", "There's no Costco in Traverse City; the closest is Grand Rapids.", "warehouse store near traverse", "para", 270),
    ("How long is the RV", "The RV is 36 feet bumper to bumper.", "length of the motorhome", "para", 350),
    ("What did I decide about the trip to Arizona", "You decided to leave for Arizona on November 3rd.", "winter travel plans out west", "concept", 18),
    ("Note that the fridge runs warm on battery", "Noted: the fridge is warm when running off the inverter.", "refrigerator not cold off grid", "para", 55),
    ("What's my pharmacy's phone number", "Walgreens in Cadillac: 231-555-0142.", "number for the drugstore", "para", 125),
    ("Which brake controller setting did we use", "Gain at 6.5, boost level 2.", "trailer brake gain", "lex", 200),
    ("Remember Pat's coffee order", "Pat takes an oat milk latte, no sugar.", "what does Pat drink", "para", 80),
    ("What's the name of the hiking trail by the lake", "The Lakeshore Loop, 3.2 miles.", "walking path near the water", "para", 230),
    ("How much was the new windshield", "The windshield replacement was 1,350 dollars.", "glass replacement price", "para", 145),
    ("When does the extended warranty end", "The extended warranty ends August 2027.", "coverage expiration", "concept", 290),
    ("What's the serial number on the generator", "Onan serial K190012345.", "onan serial", "lex", 175),
    ("Remind me how to reset the inverter", "Hold the power button 10 seconds, then wait for the green light.", "rebooting the inverter", "para", 35),
    ("What's the best time to call Grandma", "Grandma is free after 4 on weekdays.", "when can I reach my grandmother", "concept", 105),
]


# --- long meeting summaries ---------------------------------------------------------------

PROJECTS = [
    "Fiber rollout", "Website redesign", "Billing migration", "Mobile app", "Warehouse move",
    "Support desk", "Data platform", "Partner portal", "Office lease", "Hiring plan",
    "Security audit", "Pricing change", "Onboarding flow", "Search revamp", "Payments API",
    "Field service", "Analytics dashboard", "Email campaign", "Fleet tracking", "Customer forum",
    "Inventory system", "Expense tool", "Voice gateway", "Status page", "Training program",
]
VENDORS = ["Acme", "Brightline", "Northwind", "Contoso", "Globex", "Initech", "Umbrella", "Stark", "Wayne", "Hooli"]
PEOPLE_LONG = ["Pat", "Sam", "Jordan", "Alex", "Dana", "Morgan", "Riley", "Casey", "Jamie", "Lee", "Taylor", "Chris"]
RISKS = ["the vendor slipping past the holidays", "losing the only engineer who knows the old system",
         "the license costs doubling next year", "customers churning during the switch",
         "the permit taking longer than planned", "a security review blocking launch",
         "the data import corrupting old records", "nobody owning support after launch"]
FEATURES = ["single sign-on", "an export to spreadsheets", "dark mode", "bulk editing", "a mobile view",
            "audit logs", "two-factor login", "custom fields", "a public API", "scheduled reports"]
DELIVERABLES = ["project plan", "cost estimate", "draft contract", "test report", "launch checklist",
                "design mockups", "security questionnaire", "training slides", "status memo", "budget sheet"]
AREAS = ["the backend work", "the design work", "the vendor talks", "the testing", "the rollout"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "November"]
REASONS = ["they could start two months sooner", "their quote was a third lower",
           "their support team is local", "they already passed our security review"]
FILLER = [
    "We went over the notes from last week and nobody had changes.",
    "Everyone agreed to keep the scope small for now.",
    "There was a long back and forth about the order of the work.",
    "The group wants a shorter update next time.",
    "A few people joined late because of another call.",
    "We spent some time on what went well last quarter.",
    "Someone raised the question of documentation, and it was parked for later.",
    "The tone of the meeting was upbeat overall.",
    "There was general agreement that communication has improved.",
    "We looked at the shared board and moved a few cards around.",
    "The slides from the last review were shown again for context.",
    "Most of the discussion stayed at a high level.",
]

# (heading, fact, question): {p} the project (named only in the title and the opening
# line, as real summaries do), other fields filled per meeting.
SECTIONS: list[tuple[str, str, str]] = [
    ("Budget", "The budget is {money} dollars, with {cont} set aside for contractors.",
     "how much is set aside for contractors on {p}"),
    ("Vendor decision", "We chose {v1} over {v2} because {reason}.",
     "which vendor did we pick for {p} and why"),
    ("Risks", "The biggest risk is {risk}.", "what is the biggest risk to {p}"),
    ("Timeline", "The launch moved to {month} {day}.", "when is {p} launching now"),
    ("Staffing", "{lead} will lead {area}, and {new} joins the team in {month2}.",
     "who is joining the {p} team"),
    ("Customer feedback", "The pilot customers asked most for {feature}.",
     "what did pilot customers ask for on {p}"),
    ("Action items", "{owner} will send the {deliv} by Friday.", "who is sending what by Friday for {p}"),
    ("Open questions", "We still don't know whether it needs a second {vendorless} review.",
     "what is still unresolved on {p}"),
]
MEETINGS = 4  # weekly syncs per project, the same title each week
PER_MEETING = 4  # sections each one covers, of the eight


def long_docs() -> tuple[list[tuple[str, str, str, int]], list[tuple[str, frozenset[str], str]]]:
    """(id, title, body, weeks ago) for each meeting summary, and (query, the meetings
    that answer it, section) for each question: two sections asked of every project,
    answered by the project's meetings that cover that section (each covers four of
    eight, so some of its same-titled meetings do and some don't). The same every call."""
    r = random.Random(11)
    docs: list[tuple[str, str, str, int]] = []
    needles: list[tuple[str, frozenset[str], str]] = []
    for i, p in enumerate(PROJECTS):
        has: dict[int, set[str]] = {}
        for w in range(MEETINGS):
            v1, v2 = r.sample(VENDORS, 2)
            f = {
                "p": p, "money": f"{r.randint(20, 400)},000", "cont": f"{r.randint(2, 19)},000",
                "v1": v1, "v2": v2, "reason": r.choice(REASONS), "risk": r.choice(RISKS),
                "month": r.choice(MONTHS), "day": r.randint(1, 28), "lead": r.choice(PEOPLE_LONG),
                "area": r.choice(AREAS), "new": r.choice(PEOPLE_LONG), "month2": r.choice(MONTHS),
                "feature": r.choice(FEATURES), "owner": r.choice(PEOPLE_LONG),
                "deliv": r.choice(DELIVERABLES), "vendorless": r.choice(["legal", "security", "finance", "privacy"]),
            }  # fmt: skip
            id = f"granola:synth{i}w{w}"
            parts = [f"Weekly sync on {p}. Notes from the call, by section."]
            for j in r.sample(range(len(SECTIONS)), PER_MEETING):
                head, fact, _ = SECTIONS[j]
                pad = r.sample(FILLER, r.randint(5, 10))
                at = r.randint(0, len(pad))
                parts.append(f"### {head}\n" + " ".join([*pad[:at], fact.format(**f), *pad[at:]]))
                has.setdefault(j, set()).add(id)
            docs.append((id, f"{p} weekly sync", "\n\n".join(parts), w + 1))
        for j in r.sample(sorted(has), 2):
            needles.append((SECTIONS[j][2].format(p=p), frozenset(has[j]), SECTIONS[j][0]))
    return docs, needles
