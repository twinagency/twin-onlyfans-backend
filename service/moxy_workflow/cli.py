"""Local simulator: chat with the workflow as a fan. Nothing is sent anywhere.

  python -m moxy_workflow.cli --persona PATH --playbook PATH [--playbook OVERRIDE] [--state DIR] [--fan ID]

Type a fan message, or a command:  /buy <id> <dollars>   /label <emoji>   /vip   /state   /reset   /quit
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .engine import Engine
from .persona import load_persona
from .playbook import load_playbook
from .state import FanState, JsonStateStore
from .voice import money


def _show(d):
    tag = {"reply": "", "skip": "[SKIP] ", "escalate": "[ESCALATE] "}[d.action]
    print(f"  ({d.scenario}) {tag}{d.reason}".rstrip())
    for m in d.messages:
        print(f"  HER: {m}")
    if d.offer:
        print(f"  OFFER: {d.offer['kind']} {d.offer['id']} {d.offer['label']!r} {money(d.offer['price_cents'])}")
    for n in d.notes:
        print(f"  NOTE: {n}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", required=True)
    ap.add_argument("--playbook", action="append", required=True)
    ap.add_argument("--state", default=None, help="directory for fan state (default: in memory)")
    ap.add_argument("--fan", default="sim-fan")
    a = ap.parse_args(argv)
    persona = load_persona(a.persona)
    eng = Engine(persona, load_playbook(*a.playbook))
    store = JsonStateStore(a.state) if a.state else None
    st = store.load(persona.creator_id, a.fan) if store else FanState(persona.creator_id, a.fan)
    print(f"Simulating {persona.display_name}. /quit to stop.")
    while True:
        try:
            line = input("FAN> ").strip()
        except EOFError:
            break
        if not line:
            continue
        if line == "/quit":
            break
        if line == "/state":
            print(json.dumps(asdict(st), ensure_ascii=False, indent=1))
            continue
        if line == "/reset":
            st = FanState(persona.creator_id, a.fan)
        elif line == "/vip":
            st.vip = True
        elif line.startswith("/label "):
            st.labels.append(line.split(" ", 1)[1].strip())
        elif line.startswith("/buy "):
            _, oid, dollars = line.split()
            _show(eng.on_purchase(st, oid, int(round(float(dollars) * 100))))
        else:
            _show(eng.on_message(st, line))
        if store:
            store.save(st)


if __name__ == "__main__":
    main()
