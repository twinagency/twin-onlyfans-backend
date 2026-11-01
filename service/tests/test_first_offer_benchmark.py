"""R5 item 8: the pacing benchmark is read-only, counts grouped turns and raw bubbles separately, splits first/repeat."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import first_offer_benchmark as fob  # noqa: E402


def conv(*items):
    return [(d, c, b) for d, c, b in items]


def test_card_gaps_split_first_and_repeat_and_exclude_the_card():
    rows = conv(("in", False, "hi"), ("out", False, "hey you"), ("out", False, "what are you up to"), ("in", False, "nothing"),
                ("out", False, "same here lol"), ("out", True, "unlock"), ("in", False, "wow"), ("out", False, "thank you babe"),
                ("out", True, "another one"))
    assert fob.card_gaps(rows) == [(True, 2, 3), (False, 1, 1)]


def test_conversation_without_a_card_counts_in_the_denominator():
    s = fob.summarize({"a": [conv(("in", False, "hi"), ("out", False, "hello there")),
                             conv(("in", False, "hi"), ("out", False, "hello"), ("out", True, "unlock"))]})["a"]
    assert (s["conversations_with_creator_message"], s["conversations_reaching_a_card"], s["coverage"]) == (2, 1, 0.5)
    assert s["first_offer"]["grouped_turns"]["median"] == 1 and s["repeat_offer"]["grouped_turns"] is None


def test_load_is_select_only_and_never_prints_ids(tmp_path, capsys):
    path = tmp_path / "b.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE messages(account_id TEXT,fan_id INT,message_id INT,direction TEXT,price_cents INT,body TEXT,"
                "automation_kind TEXT,is_unsent BOOLEAN,created_at TEXT)")
    for i, (d, p, b, k) in enumerate([("in", 0, "hi", None), ("out", 0, "hey you", None), ("out", 500, "unlock", "ai_chatter")]):
        con.execute("INSERT INTO messages VALUES('secret-acct',777,?,?,?,?,?,0,?)", (i, d, p, b, k, "2026-01-01 00:0%d" % i))
    con.commit()
    con.close()
    fob.main(["--db", "sqlite:///" + str(path)])
    out = capsys.readouterr().out
    assert "secret-acct" not in out and "777" not in out and "creator_1" in out
    fob.main(["--db", "sqlite:///" + str(path), "--human-only"])       # the automated card is excluded -> no card reached
    assert '"conversations_reaching_a_card": 0' in capsys.readouterr().out
    assert sqlite3.connect(path).execute("SELECT count(*) FROM messages").fetchone()[0] == 3
