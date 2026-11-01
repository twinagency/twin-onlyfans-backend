"""CLI: python ../scripts/onlyfans_eval/run.py --cases PATH [--out DIR] [--generations N] [--transport synthetic|openrouter] ...

Run from service/ with PYTHONPATH=. . Never touches an OnlyFans client or sender. The real-model transport refuses to run
unless --allow-spend, --max-usd N, --budget-state PATH and the key environment variable are all present
(and the estimate fits under the cap). Both routes and every resume must share the same budget journal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "onlyfans_eval"

from onlyfans_eval import bootstrap          # noqa: E402

bootstrap()

from onlyfans_eval import assemble, cost, guards, scorer, transports   # noqa: E402


def load_cases(path):
    cases = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    ids = [c["case_id"] for c in cases]
    if len(set(ids)) != len(ids):
        raise SystemExit("duplicate case ids")
    return cases


def one_generation(case, gen_index, *, transport, route, base_seed, save_prompts=None):
    seed = base_seed * 1000 + gen_index
    params = {**transports.SAMPLING}
    rec = {"case_id": case["case_id"], "generation": gen_index, "seed": seed, "sampling_params": params,
           "model": transports.MODEL_ID, "route": route, "transport": transport.name}
    try:
        messages, meta, ctx = assemble.build_prompt(case, route=route, seed=seed)
    except Exception as e:                                              # a crash here is a FINDING
        rec.update(status="assembly_error", error=f"{type(e).__name__}: {e}", trace=traceback.format_exc(limit=6)[-1500:],
                   raw_reply=None, post_guard_text="", bubbles=[], gate=None, verdicts={})
        return rec
    rec["prompt_hash"] = assemble.prompt_hash(messages)
    rec["prompt_tokens_approx"] = assemble.approx_tokens(messages)
    rec["prompt_meta"] = meta
    if save_prompts is not None:
        save_prompts.write(json.dumps({"case_id": case["case_id"], "generation": gen_index, "messages": messages}, ensure_ascii=False) + "\n")
    gate = guards.pre_gate(case)
    rec["gate"] = gate
    if gate["action"] == "hold":
        rec.update(status="held_before_model", raw_reply=None, post_guard_text="", bubbles=[], verdicts={})
        return rec
    try:
        g = transport.generate(messages, case=case, seed=seed, params=params, output_json=(route == "worker"),
                               last_in=ctx[0].last_body)
    except transports.SpendRefused:
        raise
    except Exception as e:
        rec.update(status="transport_error", error=f"{type(e).__name__}: {e}", raw_reply=None, post_guard_text="", bubbles=[], verdicts={})
        return rec
    rec["usage"] = {"prompt_tokens": g.prompt_tokens, "completion_tokens": g.completion_tokens, "cost_usd": g.cost_usd}
    rec["transport_meta"] = g.meta
    rec["raw_output"] = g.text
    reply = g.text
    if route == "worker":
        from onlyfans_runtime.native_memory import parse_output
        try:
            reply, _memory = parse_output(g.text, {"messages": [{"direction": "in", "body": ctx[0].last_body}]})
        except Exception as e:
            rec.update(status="invalid_native_output", error=f"{type(e).__name__}: {e}", raw_reply=None, post_guard_text="", bubbles=[], verdicts={})
            return rec
    rec["raw_reply"] = reply
    try:
        res = guards.post_guards(reply, case=case, ctx=ctx, route=route, transport=transport, messages=messages, seed=seed)
    except Exception as e:
        rec.update(status="guard_crash", error=f"{type(e).__name__}: {e}", trace=traceback.format_exc(limit=6)[-1500:],
                   post_guard_text="", bubbles=[], verdicts={})
        return rec
    rec.update(status="held_after_model" if res["held_reasons"] else "ok", post_guard_text=res["post_guard_text"],
               bubbles=res["bubbles"], verdicts=res["verdicts"], held_reasons=res["held_reasons"])
    return rec


FAULT_CHECK = {"em_dash": "em_dash", "brackets": "brackets", "ai_disclosure": "ai_disclosure", "human_denial": "identity_claim",
               "instruction_leak": "instruction_or_classifier", "classifier_leak": "instruction_or_classifier",
               "delivery_promise": "delivery_promise", "invented_price": "price_not_in", "unsolicited_rating": "unsolicited_rating",
               "endorse_essentials": "output_violation"}


def fault_table(gens):
    """Synthetic fault profile: for each injected fault class, how many were held / sanitised / escaped to the fan."""
    t = {}
    for g in gens:
        f = (g.get("transport_meta") or {}).get("synthetic_fault")
        if not f:
            continue
        row = t.setdefault(f, {"injected": 0, "held": 0, "sanitised": 0, "escaped": 0})
        row["injected"] += 1
        if g["status"] in ("held_after_model", "held_before_model"):
            row["held"] += 1
        elif any(c.startswith(FAULT_CHECK.get(f, "?")) for c in g["auto"]["hard_checks"]):
            row["escaped"] += 1
        else:
            row["sanitised"] += 1
    return t


def _estimate(cases, route, gens, args):
    toks = []
    for c in cases:
        try:
            m, _, _ = assemble.build_prompt(c, route=route)
            toks.append(assemble.approx_tokens(m))
        except Exception:
            pass
    return cost.estimate(toks, gens, completion_tokens=args.completion_tokens, repair_rate=args.repair_rate,
                         cache_hit_rate=args.cache_hit_rate), len(cases) - len(toks)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cases", required=True, help="path to eval-cases.jsonl (read by path, never committed)")
    ap.add_argument("--out", help="output directory (default: ./eval-run-<utc>); keep it OUTSIDE the repo")
    ap.add_argument("--transport", choices=("synthetic", "openrouter"), default="synthetic")
    ap.add_argument("--profile", choices=("clean", "faults"), default="clean", help="synthetic reply profile")
    ap.add_argument("--route", choices=("worker", "fastt"), default="worker")
    ap.add_argument("--generations", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--estimate", action="store_true", help="print the cost estimate and exit")
    ap.add_argument("--allow-spend", action="store_true")
    ap.add_argument("--max-usd", type=float)
    ap.add_argument("--budget-state", help="private spend journal; use the SAME path for both routes and all resumes")
    ap.add_argument("--key-env", default="OPENROUTER_API_KEY")
    ap.add_argument("--completion-tokens", type=int, default=cost.DEFAULT_COMPLETION_TOKENS)
    ap.add_argument("--repair-rate", type=float, default=cost.DEFAULT_REPAIR_RATE)
    ap.add_argument("--cache-hit-rate", type=float, default=0.0)
    ap.add_argument("--save-prompts", action="store_true", help="write prompts.jsonl (full rendered prompts) to --out")
    ap.add_argument("--merge-sheet", help="path of a filled rubric sheet CSV: with --out of an existing run, print rubric pass rates")
    args = ap.parse_args(argv)

    cases = load_cases(args.cases)
    if args.limit:
        cases = cases[: args.limit]
    if args.merge_sheet:
        out = Path(args.out)
        gens = [json.loads(l) for l in open(out / "generations.jsonl")]
        print(json.dumps(scorer.merge_sheet(args.merge_sheet, {c["case_id"]: c for c in cases}, gens), indent=1))
        return 0
    est, crashed = _estimate(cases, args.route, args.generations, args)
    if args.estimate:
        est["prompt_assembly_failures"] = crashed
        print(json.dumps(est, indent=1))
        return 0
    if args.transport == "openrouter":
        try:
            key = transports.require_spend_approval(key_env=args.key_env, allow_spend=args.allow_spend, max_usd=args.max_usd,
                                                    estimate_usd=est.get("usd"))
        except transports.SpendRefused as e:
            print(str(e), file=sys.stderr)
            return 3
        p = est["price"]
        if not args.budget_state:
            print("real model refused: --budget-state is required for a shared, durable cap", file=sys.stderr)
            return 3
        transport = transports.OpenRouterTransport(key, args.max_usd, p["in_per_1k_cents"], p["out_per_1k_cents"],
                                                   budget_state=args.budget_state)
    else:
        transport = transports.SyntheticTransport(args.profile)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out or f"eval-run-{stamp}")
    out.mkdir(parents=True, exist_ok=False)
    prompts = open(out / "prompts.jsonl", "w", encoding="utf-8") if args.save_prompts else None
    by_id = {c["case_id"]: c for c in cases}
    gens = []
    t0 = time.time()
    with open(out / "generations.jsonl", "w", encoding="utf-8") as fh:
        for c in cases:
            for i in range(1, args.generations + 1):
                rec = one_generation(c, i, transport=transport, route=args.route, base_seed=args.seed, save_prompts=prompts)
                rec["auto"] = scorer.auto_score(c, rec)
                gens.append(rec)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if prompts:
        prompts.close()
    agg = scorer.aggregate(by_id, gens)
    agg["fault_injection"] = fault_table(gens)
    (out / "score.json").write_text(json.dumps(agg, indent=1, ensure_ascii=False))
    status = {}
    for g in gens:
        status[g["status"]] = status.get(g["status"], 0) + 1
    meta = {"started_utc": stamp, "seconds": round(time.time() - t0, 1), "cases_file_sha256": hashlib.sha256(Path(args.cases).read_bytes()).hexdigest(),
            "cases": len(cases), "generations_per_case": args.generations, "transport": transport.name, "profile": getattr(transport, "profile", None),
            "route": args.route, "base_seed": args.seed, "model": transports.MODEL_ID, "sampling_params": transports.SAMPLING,
            "status_counts": status, "estimate_for_this_run": est}
    (out / "run-meta.json").write_text(json.dumps(meta, indent=1))
    (out / "score.json").write_text(json.dumps(agg, indent=1, ensure_ascii=False))
    scorer.write_sheet(out / "rubric-sheet.csv", by_id, gens)
    print(json.dumps({"out": str(out), "status_counts": status, "auto_pass_all": agg["auto_pass_all"],
                      "hard_fail_generations": agg["hard_fail_generations"], "hard_fail_by_category": agg["hard_fail_by_category"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
