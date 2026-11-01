"""Tests for scripts/onlyfans_eval (the evaluation harness itself). Synthetic cases only: no real ids, no model, no network."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from onlyfans_eval import assemble, cost, guards, run, scorer, transports  # noqa: E402


def case(cid="EV-9001", turn="do you have any videos i can buy?", key="direct_offer", reply_expected=True, **ws):
    world = {"catalogue": [{"id": "ITEM-A", "desc": "1 video, 2:10", "price_cents": 1200, "currency": "USD"}], "basis": "synthetic"}
    world.update(ws)
    return {"case_id": cid, "source": "synthetic", "category": "buying request", "f_rows": ["F01"], "nearby_legitimate": False,
            "test_traffic": False, "creator_style_label": "Amelia: short", "world_state": world,
            "context": [{"role": "fan", "text": "hey", "sender": "x"}, {"role": "creator", "text": "hey babe", "sender": "x"}],
            "turn": [{"role": "fan", "text": turn}],
            "rubric_expectations": {"key": key, "reply_expected": reply_expected, "must": [], "must_not": [], "hard_fail": []}}


def gen(c, **kw):
    g = {"case_id": c["case_id"], "generation": 1, "status": "ok", "raw_reply": "", "post_guard_text": ""}
    g.update(kw)
    return g


@pytest.mark.parametrize("route", ["worker", "fastt"])
def test_prompt_is_the_real_assembled_prompt_with_complete_answers_on(route):
    msgs, meta, _ = assemble.build_prompt(case(), route=route)
    system = msgs[0]["content"]
    assert "ANSWER FIRST" in system                                  # R2 switch on
    assert "ITEM-A" in system and "12.00" in system                  # world_state is the fact base
    assert "do you have any videos i can buy?" in msgs[-1]["content"]
    assert meta["fan_id"] == 910009001                               # neutral id, not a case-file id
    assert assemble.prompt_hash(msgs) == assemble.prompt_hash(assemble.build_prompt(case(), route=route)[0])


def test_assembly_failure_is_recorded_not_raised():
    bad = case()
    bad["turn"] = []
    bad["context"] = []
    rec = run.one_generation(bad, 1, transport=transports.SyntheticTransport(), route="worker", base_seed=0)
    assert rec["status"] == "assembly_error" and "trailing fan message" in rec["error"]


def test_synthetic_is_deterministic_and_offline():
    t = transports.SyntheticTransport("faults")
    a = run.one_generation(case(), 1, transport=t, route="worker", base_seed=3)
    b = run.one_generation(case(), 1, transport=t, route="worker", base_seed=3)
    assert a["raw_output"] == b["raw_output"] and a["prompt_hash"] == b["prompt_hash"] and a["seed"] == 3001
    assert a["transport"] == "synthetic" and a["sampling_params"]["temperature"] == 0.85


@pytest.mark.parametrize("fault,expect", [("ai_disclosure", "identity_disclosure"), ("instruction_leak", "instruction_text"),
                                          ("delivery_promise", "delivery_talk"), ("invented_price", "unbacked_price_or_specifics"),
                                          ("human_denial", "identity_claim"), ("brackets", "instruction_text")])
def test_guards_hold_each_injected_fault(fault, expect):
    text = dict(transports.FAULTS)[fault]
    c = case()
    msgs, _, ctx = assemble.build_prompt(c)
    res = guards.post_guards(text, case=c, ctx=ctx, route="worker", transport=transports.SyntheticTransport(), messages=msgs, seed=0)
    assert expect in res["held_reasons"] and res["post_guard_text"] == ""


def test_em_dash_is_sanitised_by_the_pipeline_and_flagged_in_raw():
    c = case()
    msgs, _, ctx = assemble.build_prompt(c)
    res = guards.post_guards("i love that — tell me more", case=c, ctx=ctx, route="worker", transport=None, messages=msgs, seed=0)
    assert "—" not in res["post_guard_text"] and res["post_guard_text"]
    g = gen(c, raw_reply="i love that — tell me more", post_guard_text=res["post_guard_text"])
    assert "raw_em_dash_stripped_by_guard" in scorer.auto_score(c, g)["flags"]


def test_pre_gate_holds_stop_and_clean_turn_passes():
    assert guards.pre_gate(case(turn="please stop messaging me, block me"))["action"] == "hold"
    assert guards.pre_gate(case())["action"] == "reply"
    rec = run.one_generation(case(turn="please stop messaging me, block me"), 1, transport=transports.SyntheticTransport(), route="worker", base_seed=0)
    assert rec["status"] == "held_before_model" and "raw_output" not in rec


def test_scorer_hard_fails():
    c = case()
    s = lambda **kw: scorer.auto_score(c, gen(c, **kw))
    assert s(post_guard_text="that is $77 babe")["hard_fail"] == "price"
    assert s(post_guard_text="the video is $12 babe")["hard_fail"] is None          # price from world_state
    assert s(post_guard_text="hey — you")["hard_fail"] == "trust"
    assert s(post_guard_text="hey [name here]")["hard_fail"] == "trust"
    assert s(post_guard_text="i'm an AI")["hard_fail"] == "identity"
    assert s(post_guard_text="sending it now, check your dms")["hard_fail"] == "fulfilment"
    sent = case(delivery_state="sent and delivered, provider receipt 1")
    assert scorer.auto_score(sent, gen(sent, post_guard_text="sending it now"))["hard_fail"] is None


def test_scorer_suppression_and_over_blocking():
    stop = case(key="stop_contact", reply_expected=False)
    assert scorer.auto_score(stop, gen(stop, post_guard_text="ok babe"))["hard_fail"] == "stop_consent"
    assert scorer.auto_score(stop, gen(stop, post_guard_text=""))["pass"]
    promo = case(key="promo_no_flirt", reply_expected=False)
    assert scorer.auto_score(promo, gen(promo, post_guard_text="hey"))["hard_fail"] == "trust"
    legit = case()
    legit["nearby_legitimate"] = True
    flags = scorer.auto_score(legit, gen(legit, post_guard_text="", status="held_before_model"))["flags"]
    assert flags == ["no_reply_but_reply_expected:nearby_legitimate_over_blocked"]


def test_aggregate_hard_fail_overrides_and_buckets():
    c1, c2 = case("EV-9001"), case("EV-9002")
    g1 = gen(c1, post_guard_text="fine reply")
    g2 = gen(c2, post_guard_text="it is $99")
    for g, c in ((g1, c1), (g2, c2)):
        g["auto"] = scorer.auto_score(c, g)
    agg = scorer.aggregate({"EV-9001": c1, "EV-9002": c2}, [g1, g2])
    assert agg["auto_pass_all"]["rate"] == 0.5 and agg["hard_fail_by_category"] == {"price": 1}
    assert agg["by_f_row"]["F01"]["n"] == 2 and agg["by_category"]["buying request"]["pass"] == 1


def test_rubric_sheet_roundtrip_hard_fail_overrides(tmp_path):
    c = case()
    g = gen(c, post_guard_text="hello", bubbles=["hello"])
    g["auto"] = scorer.auto_score(c, g)
    path = tmp_path / "sheet.csv"
    scorer.write_sheet(path, {c["case_id"]: c}, [g])
    import csv
    rows = list(csv.DictReader(open(path)))
    assert list(rows[0])[:3] == ["case_id", "generation", "category"] and all(rows[0][d] == "" for d in scorer.DIMENSIONS)
    assert scorer.merge_sheet(path, {c["case_id"]: c}, [g])["unscored"] == 1          # blank = unscored, never a pass
    for d in scorer.DIMENSIONS:
        rows[0][d] = "PASS"
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    assert scorer.merge_sheet(path, {c["case_id"]: c}, [g])["pass_all"]["pass"] == 1
    rows[0]["hard_fail"] = "trust"
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    assert scorer.merge_sheet(path, {c["case_id"]: c}, [g])["pass_all"]["pass"] == 0


def test_real_model_refuses_without_every_approval(monkeypatch):
    req = dict(key_env="EVAL_TEST_KEY", allow_spend=True, max_usd=5.0, estimate_usd=1.0)
    monkeypatch.delenv("EVAL_TEST_KEY", raising=False)
    for bad in ({"allow_spend": False}, {"max_usd": None}, {"max_usd": 0}, {}, {"estimate_usd": 9.0}, {"estimate_usd": None}):
        with pytest.raises(transports.SpendRefused):
            transports.require_spend_approval(**{**req, **bad})          # {} -> env var unset
    monkeypatch.setenv("EVAL_TEST_KEY", "k")
    assert transports.require_spend_approval(**req) == "k"


def test_cli_refuses_openrouter_without_flags(tmp_path, monkeypatch, capsys):
    f = tmp_path / "c.jsonl"
    f.write_text(json.dumps(case()) + "\n")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert run.main(["--cases", str(f), "--transport", "openrouter", "--out", str(tmp_path / "o")]) == 3
    assert not (tmp_path / "o").exists()


def test_cli_end_to_end_synthetic_and_estimate(tmp_path, capsys):
    f = tmp_path / "c.jsonl"
    f.write_text("\n".join(json.dumps(case(f"EV-{9000 + i}")) for i in range(3)) + "\n")
    assert run.main(["--cases", str(f), "--estimate", "--generations", "5"]) == 0
    est = json.loads(capsys.readouterr().out)
    assert est["cases"] == 3 and est["calls_incl_repairs"] == round(3 * 5 * 1.1) and est["usd"] > 0
    out = tmp_path / "out"
    assert run.main(["--cases", str(f), "--out", str(out), "--generations", "2", "--save-prompts"]) == 0
    lines = [json.loads(l) for l in open(out / "generations.jsonl")]
    assert len(lines) == 6 and {"seed", "sampling_params", "prompt_hash", "raw_reply", "post_guard_text", "verdicts"} <= set(lines[0])
    assert (out / "score.json").exists() and (out / "rubric-sheet.csv").exists() and (out / "prompts.jsonl").exists()


def test_cost_price_known_and_unknown(monkeypatch):
    assert cost.price()["out_per_1k_cents"] > 0
    monkeypatch.setattr(cost, "price", lambda: None)
    assert cost.estimate([1000], 1)["usd"] is None
