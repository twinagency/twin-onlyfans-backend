"""OnlyFans "complete answers" scope for the shared reply prompt.

WHY THIS FILE EXISTS. ``ai_chatter`` / ``welcome_chatter_for_info`` / ``autoreply`` / ``_voice`` are shared with
FLIRT and Telegram. Their prompts govern brevity ("you write the LEAST you can", "a line that PUNCHES beats one
that explains", tiny-reaction style dice, an interview loop, "ONE short line" on every special turn). Those are
right for a flirt bot and wrong for an OnlyFans seller that has to answer a product question, a price objection or a
buying request. Rewriting the shared templates would change the other products, so this module holds every
OnlyFans-only replacement and the caller opts in with ONE switch: the existing ``native_reply_format`` capability
(``reply_format=True`` on ``_build_messages``, ``cfg["_native_reply_format"]`` inside ``run``), extended from
"ordinary turns only" to "complete answers on every route".

Everything here is pure (no I/O). With the switch off none of it is reached, so every other caller's prompt is
byte-identical (pinned by ``tests/test_r2_complete_answers.py``).

WHAT IS DELIBERATELY NOT HERE: a paragraph quota, a length-matching rule or forced multi-bubble output. A one-line
reply that fully answers the turn stays a one-line reply.
"""
from __future__ import annotations

import re

# One shared rule, rendered once for every turn kind (the per-kind blocks keep their own mechanics).
COMPLETE_ANSWER_RULE = (
    "ANSWER FIRST: answer every question and request in his unanswered messages, in the order he sent them, "
    "before anything else. Use the words a complete answer needs, no padding. A clarifying question is only for "
    "something you truly cannot answer without it, and it never replaces the answer. Never ask for anything "
    "listed under 'What you know about him' or anything he already said in this chat. If you asked him for "
    "something (a please, a tip, a picture, a name) and he did it, acknowledge it and move on; never ask again. "
    "Never reuse a pet name, challenge, sign-off or closing question you already used in this chat. When he "
    "objects, corrects you or says he cannot afford something, answer respectfully and plainly: no mockery, "
    "no insults, and take his correction as given. Stay in the language of his latest message, keep tenses "
    "straight (if he is about to do something it is not done yet), and keep the roles straight: you are the "
    "creator, he is the customer, never swap who said what."
)

# The ordinary-turn need_block (kept from the first cut of native_reply_format; test_native_reply_format pins it).
ORDINARY_NEED = "Answer what he said. Ask for clarification only if it is needed to answer accurately."

# The bare style-slot replacement already shipped as ai_chatter.REPLY_FORMAT_INSTRUCTION (re-exported there).

VOICE_LINE = (
    "- Write in the creator's casual voice (lowercase and u/ur are fine) but never let the style cut an answer "
    "short or leave a question unanswered. React to what he said first.\n"
)
VARY_LINE = ("- VARY it — never reuse an opener, phrase, pet name, challenge or emoji from this chat.\n")
FOLLOWUP_LINE = "- Ask a follow-up only when useful; never repeat an answered question.\n"

# Per-kind text: the kind's mechanics survive, its brevity / forced-question wording does not.
BRUSH_OFF_TRUTHFUL = (
    "He asked whether you're real or an AI. Do not confirm or deny it, never mention AI, bots, clones or "
    "automation, and never swear you are human. Answer anything else he asked, then something HE told you. "
    "No sale."
)
BRUSH_OFF_PLAIN = (
    "He thinks you might be a bot — defending is what a bot does. Brush it off lightly, answer anything else he "
    "asked, then something HE told you. No sell."
)
REACT_PIC_TAIL = " WARM, not sexual, never invent what he sent."
RATE_PIC_TAIL = ("\nWrite it the way you'd type it. GOOD: "
                 '"grooming\'s good, glad ur not fully shaved" / "8.5 — losing half a '
                 'point cause ur not all the way hard yet, i wanna see that"\n'
                 "Never name anything not in the description.")
ESCALATION = ("HE'S CLEARLY INTO IT RIGHT NOW — the moment to SELL, not tease. Match his heat, answer anything he "
              "asked, then close it: {close}")
PIC_OFFER = (
    "HE JUST OFFERED TO SEND YOU A PICTURE OF HIMSELF, or asked you to rate "
    "him. SAY YES and TELL HIM TO SEND IT — imperative ('send it', 'lemme "
    "see'), never coy: a 'maybe i do' sends nothing. Promise the rating — "
    "what you see, a score out of ten — and say what you want it OF. Match "
    "the thread's register. An invitation, not a sale: no price, no offer line."
)
DARE = (
    "He questioned whether you're real last message and you laughed it off. "
    "Now DARE him: send a picture right now and you'll tell him exactly "
    "what you see — a bot couldn't. Promise a rating out of ten; filthy "
    "about what you want a picture OF if the thread's explicit, cheeky "
    "if tame. A dare, not a sale: no price, no offer."
)
HOT_HEAD = (
    "THE THREAD IS HOT — a live sexual scene RIGHT NOW. The one way to "
    "throw it away is a get-to-know question (job, day, city, age) — "
    "never. Answer anything he asked first, then run the scene:\n"
    "1. STAY IN IT — present tense, first person, dirty.\n"
    "2. IF HE HASN'T SAID WHAT HE WANTS yet, make him say it ('what would you do to me if you were "
    "here right now?') — his answer is the whole game; if he already told you, do not ask again.\n"
    "3. MIRROR IT BACK, HOTTER — his words escalated, as if it's already "
    "happening."
)

_HUMANIZER_REWRITES = (
    # (line prefix, replacement). The first match wins; a line that is not found is simply left alone, so a voice
    # variant that words it differently is never half-edited.
    ("- vary length wildly.",
     "- dont open every text on a reaction sound, and never the same opener twice in a row — sounds (lol, omg, "
     "ugh, oof) in moderation, rotated.\n"),
    ("- never explain yourself.",
     "- explain when he asks you to. 0-1 emoji, never the same emoji twice."),
)


def humanizer(text: str) -> str:
    """The shared "text like a real person" block with its two brevity governors rewritten.

    ``text`` is ``VoiceBlocks.humanizer`` (female and male variants). Only whole lines starting with the known
    prefixes are replaced; the rest (em-dash ban, never echo him, typo allowance, emoji vocabulary) is the voice
    owner's and stays untouched."""
    out = []
    lines = text.split("\n")
    # the "vary length wildly" sentence continues on the next physical line in the shipped block
    i = 0
    while i < len(lines):
        line = lines[i]
        rewritten = None
        for prefix, replacement in _HUMANIZER_REWRITES:
            if line.startswith(prefix):
                rewritten = replacement
                break
        if rewritten is None:
            out.append(line)
        else:
            out.append(rewritten.rstrip("\n"))
            if line.startswith("- vary length wildly.") and i + 1 < len(lines) and lines[i + 1].startswith("never the same opener"):
                i += 1                      # the continuation is part of the replaced sentence
        i += 1
    return "\n".join(out)


# ── The unanswered inbound sequence ──────────────────────────────────

_MAX_RUN = 6
_MAX_LINE = 300


def unanswered_run(history) -> list[str]:
    """His trailing inbound messages since the last outbound, oldest first (bodies as the prompt shows them)."""
    run: list[str] = []
    for direction, body in reversed(history):
        if direction == "in" and body:
            run.append(body)
        elif run:
            break
    run.reverse()
    return run[-_MAX_RUN:]


def has_question(text: str) -> bool:
    return "?" in text


def render_unanswered(run: list[str]) -> str:
    """The "answer all of these" block for the user message. Empty for one plain statement (nothing owed
    beyond the ordinary reply), so quiet turns do not grow the prompt."""
    if not run or (len(run) == 1 and not has_question(run[0])):
        return ""
    lines = "\n".join(f"{i}. {' '.join(m.split())[:_MAX_LINE]}" for i, m in enumerate(run, 1))
    return ("HIS UNANSWERED MESSAGES (answer every question and request in them, in this order, before anything "
            f"else):\n{lines}\n\n")


# ── What is already known (never re-ask) ─────────────────────────────

_ASK_TOPICS = (
    ("name", re.compile(r"\b(?:what(?:'?s| is)? (?:ur|your) name|what should i call (?:u|you)|who (?:are|r) (?:u|you)\b"
                        r"|(?:ur|your) name\b[^.!?]*\?)", re.I)),
    ("age", re.compile(r"\b(?:how old (?:are|r) (?:u|you|ya)|what(?:'?s| is) (?:ur|your) age|(?:ur|your) age\b[^.!?]*\?)", re.I)),
    ("job", re.compile(r"\b(?:what do (?:u|you|ya) do (?:for (?:a )?(?:work|living|money)|at work)|what(?:'?s| is) (?:ur|your) "
                       r"(?:job|work|line of work)|where do (?:u|you) work|what (?:u|you) do for work)\b", re.I)),
    ("location", re.compile(r"\b(?:where (?:are )?(?:u|you|ya) (?:from|live|located|textin me from|based)|where(?:'?s| is) "
                            r"(?:ur|your) (?:home|city|town)|where u textin me from)\b", re.I)),
)


def asked_topics(text: str) -> set[str]:
    """Which get-to-know topics this text asks about (pure; used both for re-ask detection and the
    interview-loop check)."""
    return {name for name, rule in _ASK_TOPICS if rule.search(text or "")}


def known_topics(fan, *, name: str | None = None, given: str | None = None) -> set[str]:
    """Topics already answered on the fan row (or by a name he gave in the chat)."""
    known: set[str] = set()
    def has(value):
        return value is not None and str(value).strip() != ""
    if has(name) or has(given) or has(getattr(fan, "real_name", None)) or has(getattr(fan, "custom_nickname", None)):
        known.add("name")
    if has(getattr(fan, "his_age", None)):
        known.add("age")
    if has(getattr(fan, "occupation", None)):
        known.add("job")
    if has(getattr(fan, "home_city", None)) or has(getattr(fan, "home_country", None)):
        known.add("location")
    return known


def merge_explore(raw: str, bridge: str) -> str:
    """The EXPLORE bridge joins the answer instead of replacing it. If the answer already ends in a question,
    the bridge is not stacked on top (one question per reply); an empty answer becomes the bridge."""
    raw, bridge = (raw or "").strip(), (bridge or "").strip()
    if not raw:
        return bridge
    if not bridge or bridge in raw:
        return raw
    if bridge.endswith("?") and raw.endswith("?"):
        return raw
    return raw + "\n\n" + bridge
