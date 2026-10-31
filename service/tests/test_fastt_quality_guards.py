"""Reply quality guards with transcripts: the name a fan gave, memory with canonical facts, "a real person", false denials."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + tempfile.mkdtemp() + "/q.db"

CHECKS = 0


def check(v, label=""):
    global CHECKS
    assert v, label
    CHECKS += 1


NAME_TRANSCRIPTS = [
    (["hey", "I'm Jesse btw"], "Jesse"),
    (["I'm Ronan", "no it's Jesse, not Ronan"], "Jesse"),                  # a correction: the latest wins
    (["my name is mark", "lol call me Marcus"], "Marcus"),
    (["hi its Jesse not ronan"], "Jesse"),
    (["im good", "I'm tired", "I'm Horny", "its late", "I'm here"], None),  # mood words are never names
    (["call me Dave"], "Dave"),
    (["this is Tom", "you can call me Tommy"], "Tommy"),
    (["what's your name?"], None),
]

ASKS_FOR_HUMAN = ["i want a real person", "can I talk to a real person?", "let me talk to a human", "is this really her?",
                  "get me the creator", "I need a human", "i want to speak to the creator", "can i speak with a person",
                  "are you a bot?", "is this ai?"]
NOT_ASKING = ["send me something", "you're so real lol", "are you really into me", "my friend is a real person", "I talked to a person today",
              "haha love it", "what are you wearing"]
FALSE_DENIALS = ["im a real person babe", "I'm not a bot lol", "it's really me typing", "no one else is typing", "I'm typing this myself",
                 "not a robot baby", "I am human", "you're not talking to a bot"]
FINE_REPLIES = ["haha ur cute", "i love ai art tho", "im all yours tonight", "the robot vacuum died lol", "im so real with u",
                "i am a real fan of that", "I'm not sure", "no ai art lol", "mmm tell me more"]


def names():
    from native_pilot.fan_name import given_name
    for transcript, expected in NAME_TRANSCRIPTS:
        check(given_name(transcript) == expected, (transcript, given_name(transcript), expected))


def identity():
    from native_pilot.identity import denies_automation, sincere_identity_question, wants_human
    for text in ASKS_FOR_HUMAN:
        check(sincere_identity_question(text), text)
    for text in NOT_ASKING:
        check(not wants_human(text), text)
    for text in FALSE_DENIALS:
        check(denies_automation(text), text)
    for text in FINE_REPLIES:
        check(not denies_automation(text), text)


def prompt():
    from automations import ai_chatter as ac
    from db.models import Fan
    f = Fan(account_id="a", fan_id=1, his_age=33, home_city="Perth", occupation="plumber", hobbies="surf", home_country="AU")
    c = ac._Cand(1)
    c.last_in_text = "hey"

    def facts(**kw):
        msgs, _ = ac._build_messages("PERSONA", f, c, set(), twin_context=True, **kw)
        return "\n".join(m["content"] for m in msgs)
    plain = facts()
    check("age: 33" in plain and "city: Perth" in plain)
    both = facts(dated_memory=True)
    check("age: 33" in both and "city: Perth" in both and "occupation: plumber" in both,
          "canonical facts stay in the prompt alongside learned memory")
    cleared = facts(dated_memory=True, suppress_fields=frozenset({"his_age", "home_city"}))
    check("age: 33" not in cleared and "city: Perth" not in cleared and "occupation: plumber" in cleared,
          "a field an operator cleared or replaced is withheld, nothing else is")
    named = facts(name_override="Jesse")
    check("name/nickname: Jesse (he said so himself" in named, "the name he gave is the name")


def isolation():
    """The name comes only from THIS account's THIS fan's own inbound messages."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from db.models import Account, Base, Fan, Message
    from native_pilot.twin_context import _given_name
    d = tempfile.mkdtemp()
    eng = create_engine("sqlite:///" + d + "/iso.db")
    Base.metadata.create_all(eng)
    now = datetime(2026, 10, 30, 12)
    with Session(eng) as s, s.begin():
        for acct in ("a1", "a2"):
            s.add(Account(id=acct))
        s.flush()
        for acct, fan in (("a1", 1), ("a1", 2), ("a2", 1)):
            s.add(Fan(account_id=acct, fan_id=fan))
        s.flush()

        def say(acct, fan, mid, text, direction="in", minutes=0):
            s.add(Message(account_id=acct, fan_id=fan, message_id=mid, direction=direction, body=text, media_ids="[]",
                          media_count=0, price_cents=0, is_tip=False, is_unsent=False, is_promise=False, sender_name="x",
                          created_at=now + timedelta(minutes=minutes), ingested_at=now + timedelta(minutes=minutes), raw_json="{}"))
        say("a1", 1, 1, "I'm Jesse")
        say("a1", 1, 2, "haha", minutes=1)
        say("a1", 1, 3, "my name is Creator", direction="out", minutes=2)      # her own words are not his name
        say("a1", 2, 4, "I'm Mark")
        say("a2", 1, 5, "I'm Sam")
    with Session(eng) as s:
        check(_given_name(s, "a1", 1) == "Jesse", "the fan's own words")
        check(_given_name(s, "a1", 2) == "Mark", "another fan of the same account")
        check(_given_name(s, "a2", 1) == "Sam", "the same fan id under another account")
        check(_given_name(s, "a1", 3) is None)
    eng.dispose()


names()
identity()
prompt()
isolation()
print(f"fastt quality guards: all cases passed ({CHECKS} checks)")
