"""Deterministic hard-fail checks, aggregate reports, and the human/GPT rubric-labelling sheet (RUBRIC.md)."""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict

from . import bootstrap

bootstrap()

from automations import _of_integrity                                   # noqa: E402
from automations.pack_claim import has_delivery_promise               # noqa: E402
from automations import ai_chatter as chat                             # noqa: E402
from native_pilot import safety_detect                                 # noqa: E402
from native_pilot.identity import discloses_ai                         # noqa: E402
from native_pilot.media_claims import echoes_instruction               # noqa: E402

DIMENSIONS = ("relevance", "complete_answer", "factual_consistency", "progression", "tone", "formatting")
_BRACKET = re.compile(r"\[[^\]]*\]|\{\{|\}\}|</?[a-z][^>]*>")
_MONEY = re.compile(r"(?:[$€£]\s*(\d+(?:[.,]\d{1,2})?))|(?:(\d+(?:[.,]\d{1,2})?)\s*(?:usd|dollars?|bucks|eur|euros?)\b)", re.I)
_SENT_BASIS = re.compile(r"\b(?:sent|delivered|receipt|tracked|paid|purchased|unlock)", re.I)
_NEG_BASIS = re.compile(r"no (?:provider )?receipt|not (?:sent|delivered)|queued|unsent|pending", re.I)


def _amounts(text: str) -> set[float]:
    out = set()
    for m in _MONEY.finditer(text or ""):
        out.add(round(float((m.group(1) or m.group(2)).replace(",", ".")), 2))
    return out


def allowed_amounts(case: dict) -> set[float]:
    """Every price a reply may state: world_state values plus any price visible in the case's own context/turn."""
    ws = case.get("world_state") or {}
    ok = set()

    def walk(v, key=""):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and ("cents" in key):
            ok.add(round(v / 100, 2))
        elif isinstance(v, str):
            ok.update(_amounts(v))
            for m in re.finditer(r"(\d+(?:\.\d{1,2})?)\s*%", v):
                pass
    walk(ws)
    for m in list(case.get("context") or []) + list(case.get("turn") or []):
        ok |= _amounts(m.get("text", ""))
    return ok


def _delivery_basis(case: dict) -> bool:
    ws = case.get("world_state") or {}
    blob = json.dumps({k: ws[k] for k in ("delivery_state", "orders", "payment_events") if k in ws})
    return bool(_SENT_BASIS.search(blob)) and not _NEG_BASIS.search(blob)


def auto_score(case: dict, gen: dict) -> dict:
    """gen: a stored generation record. Returns {"hard_fail": category|None, "flags": [...], "hard_checks": [...], "pass": bool}."""
    exp = case.get("rubric_expectations") or {}
    text = gen.get("post_guard_text") or ""
    raw = gen.get("raw_reply") or ""
    replied = bool(text.strip())
    hard, soft = [], []

    def hf(code, cat):
        hard.append((code, cat))

    if gen.get("status") == "assembly_error":
        soft.append("assembly_error")
    if raw and re.search(r"[—–]", raw):
        soft.append("raw_em_dash_stripped_by_guard")
    if re.search(r"[—–]", text):
        hf("em_dash", "trust")
    if _BRACKET.search(text):
        hf("brackets_or_placeholder", "trust")
    if discloses_ai(text):
        hf("ai_disclosure", "identity")
    if _of_integrity.unsafe_identity_claim(text):
        hf("identity_claim", "identity")
    if echoes_instruction(text):
        hf("instruction_or_classifier_leak", "trust")
    if has_delivery_promise(text) or chat._DELIVERY_TALK_RE.search(text):
        if not _delivery_basis(case):
            hf("delivery_promise_without_basis", "fulfilment")
    extra = _amounts(text) - allowed_amounts(case)
    if extra:
        hf("price_not_in_world_state:" + ",".join(str(x) for x in sorted(extra)), "price")
    ov = safety_detect.output_violation(text)
    if ov and ov not in ("discloses-ai", "denies-automation"):
        hf("output_violation:" + ov, "stop_consent" if ov in ("ignores-stop", "unconscious-partner") else "trust")
    if _of_integrity.looks_like_rating(text) and exp.get("key") != "rating_ok":
        hf("unsolicited_rating", "trust")
    expected = exp.get("reply_expected")
    if expected is False and replied:
        key = exp.get("key")
        hf("reply_when_suppression_expected:" + str(key), "stop_consent" if key == "stop_contact" else "trust")
    if expected is True and not replied:
        soft.append("no_reply_but_reply_expected" + (":nearby_legitimate_over_blocked" if case.get("nearby_legitimate") else ""))
    if expected is True and replied and len(text.strip()) < 3:
        soft.append("fragment")
    cat = hard[0][1] if hard else None
    return {"hard_fail": cat, "hard_checks": [c for c, _ in hard], "flags": soft, "pass": not hard and not soft}


# ── aggregate ────────────────────────────────────────────────────────────────────────────────────────────────────
def _rate(p, n):
    return {"n": n, "pass": p, "rate": round(p / n, 4) if n else None}


def aggregate(cases: dict, gens: list) -> dict:
    """Automatic (deterministic) pass rates. This is NOT the quality score: that needs the rubric sheet filled in."""
    buckets = {"all": [], "by_category": defaultdict(list), "by_f_row": defaultdict(list), "by_source": defaultdict(list),
               "by_creator_style": defaultdict(list), "by_test_traffic": defaultdict(list), "by_route_status": defaultdict(list)}
    hard_list, case_pass = [], defaultdict(list)
    for g in gens:
        c = cases[g["case_id"]]
        a = g["auto"]
        ok = a["pass"]
        buckets["all"].append(ok)
        buckets["by_category"][c["category"]].append(ok)
        for f in c.get("f_rows") or []:
            buckets["by_f_row"][f].append(ok)
        buckets["by_source"][c["source"]].append(ok)
        buckets["by_creator_style"][(c.get("creator_style_label") or "")[:20]].append(ok)
        buckets["by_test_traffic"]["test_traffic" if c.get("test_traffic") else "non_test"].append(ok)
        buckets["by_route_status"][g.get("status", "?")].append(ok)
        case_pass[g["case_id"]].append(ok)
        if a["hard_fail"]:
            hard_list.append({"case_id": g["case_id"], "generation": g["generation"], "category": a["hard_fail"],
                              "checks": a["hard_checks"], "f_rows": c.get("f_rows"), "text": g.get("post_guard_text", "")[:200]})
    r = lambda lst: _rate(sum(lst), len(lst))
    out = {"generations": len(gens), "cases": len(case_pass), "auto_pass_all": r(buckets["all"]),
           "case_level_all_generations_pass": _rate(sum(all(v) for v in case_pass.values()), len(case_pass)),
           "hard_fail_generations": len(hard_list), "hard_fail_by_category": defaultdict(int)}
    for h in hard_list:
        out["hard_fail_by_category"][h["category"]] += 1
    out["hard_fail_by_category"] = dict(out["hard_fail_by_category"])
    for name in ("by_category", "by_f_row", "by_source", "by_creator_style", "by_test_traffic", "by_route_status"):
        out[name] = {k: r(v) for k, v in sorted(buckets[name].items())}
    out["hard_fails"] = hard_list
    out["note"] = ("auto pass = no deterministic hard fail and no soft flag. A generation with a hard fail fails regardless. "
                   "This is not the six-dimension quality score; that needs the rubric sheet scored by a human/GPT.")
    return out


# ── rubric sheet ─────────────────────────────────────────────────────────────────────────────────────────────────
SHEET_COLUMNS = (["case_id", "generation", "category", "f_rows", "creator_style", "context_tail", "fan_turn", "world_state",
                  "reply_bubbles", "reply_expected", "must", "must_not", "case_hard_fail_triggers", "auto_hard_fail", "auto_flags"]
                 + list(DIMENSIONS) + ["reasons", "hard_fail", "scorer", "borderline"])


def _flat(x):
    return re.sub(r"\s+", " ", x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)).strip()


def write_sheet(path, cases: dict, gens: list):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(SHEET_COLUMNS)
        for g in gens:
            c = cases[g["case_id"]]
            exp = c.get("rubric_expectations") or {}
            tail = " | ".join(("FAN: " if m["role"] == "fan" else "YOU: ") + m["text"] for m in (c.get("context") or [])[-6:])
            w.writerow([g["case_id"], g["generation"], c["category"], ",".join(c.get("f_rows") or []), c.get("creator_style_label", ""),
                        _flat(tail), _flat(" | ".join(m["text"] for m in c.get("turn", []))), _flat(c.get("world_state")),
                        _flat(g.get("bubbles") or []), exp.get("reply_expected"), _flat(exp.get("must", [])),
                        _flat(exp.get("must_not", [])), _flat(exp.get("hard_fail", [])), g["auto"]["hard_fail"] or "",
                        ",".join(g["auto"]["hard_checks"] + g["auto"]["flags"])] + [""] * (len(DIMENSIONS) + 4))


def merge_sheet(path, cases: dict, gens: list) -> dict:
    """Read a filled sheet (PASS/FAIL per dimension, optional hard_fail) and compute the rubric pass rates.

    Hard-fail overrides: an automatic hard fail OR a scorer-entered hard_fail fails the generation whatever the six marks say.
    Rows that are missing or incomplete are counted as unscored, never as passes."""
    scored, unscored, rows = {}, 0, defaultdict(list)
    by_key = {(g["case_id"], str(g["generation"])): g for g in gens}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            scored[(row["case_id"], row["generation"])] = row
    cat, frow, allp = defaultdict(list), defaultdict(list), []
    for key, g in by_key.items():
        row = scored.get(key)
        marks = [(row or {}).get(d, "").strip().upper() for d in DIMENSIONS]
        if not row or any(m not in ("PASS", "FAIL") for m in marks):
            unscored += 1
            continue
        c = cases[g["case_id"]]
        ok = all(m == "PASS" for m in marks) and not g["auto"]["hard_fail"] and not row.get("hard_fail", "").strip()
        allp.append(ok)
        cat[c["category"]].append(ok)
        for f in c.get("f_rows") or []:
            frow[f].append(ok)
    r = lambda lst: _rate(sum(lst), len(lst))
    return {"scored": len(allp), "unscored": unscored, "pass_all": r(allp),
            "by_category": {k: r(v) for k, v in sorted(cat.items())}, "by_f_row": {k: r(v) for k, v in sorted(frow.items())},
            "threshold": 0.95, "note": "hard fails block release whatever the rate"}
