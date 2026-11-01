"""The workflow engine: one fan event in, one explicit Decision out. No model decides a step.

Per message, in order:
  1. Gates     escalated fan stays silent; quiet labels (⛔) never engaged; shift-skip labels (⏰ in the GY shift);
               throttle labels (📢 answer once an hour when not spending).
  2. Safety    escalation triggers stop the thread with NO reply; a chargeback gets one firm reply then escalates.
  3. Scenario  the playbook reply for the detected scenario (variants rotate, so the same line is not repeated).
  4. Selling   step-by-step and scripted: a pending invite accepted by "yes" sends that exact offer; a direct ask
               offers a matching priced item (VIP-only items only to VIPs); otherwise, after enough rapport, the
               NEXT vault PPV in order (never skipping a step or level). No price is ever invented: an item without
               an approved price produces an operator note, never a fan-facing quote.
Purchases advance the vault: all steps of a level unlock the next level; the last level makes the fan VIP.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import classify as C
from .voice import MissingFact, money, render, violates_never_say

# Scenarios after which a proactive offer would be tone-deaf.
_NO_SELL_AFTER = {C.TOO_MANY_SALES, C.FEELINGS, C.REAL_RELATIONSHIP, C.DELAYED_REPLY, C.JEALOUS, C.EXCLUSIVE,
                  C.FAMILY_COMMENT, C.SECRECY, C.IDENTITY, C.PAST_PROMISE, C.BOUNDARY_PUSH, C.OTHER_CREATOR,
                  C.CHARGEBACK, C.NO, C.PRICE_COMPARE, C.REMEMBER_PAST, C.IGNORED_QUESTION, C.CONFLICTING}


@dataclass
class Decision:
    action: str                           # "reply" | "skip" | "escalate"
    scenario: str
    messages: list = field(default_factory=list)   # fan-facing text bubbles, in order
    offer: dict | None = None             # {"kind": "vault"|"item"|"rating", "id", "label", "price_cents"}
    notes: list = field(default_factory=list)      # operator-facing notes (never sent to the fan)
    reason: str = ""


class Engine:
    def __init__(self, persona, playbook, *, rephrase=None, clock=None):
        self.p = persona
        self.book = playbook
        self.rephrase = rephrase            # optional AI hook: (text, persona) -> text. Off by default.
        self.clock = clock or datetime.now

    # ── public API ─────────────────────────────────────────────────────────────────────────────────────────────
    def on_message(self, st, text: str) -> Decision:
        now = self.clock()
        gate = self._gate(st, now)
        if gate:
            return gate
        hit = C.classify(text)
        sc = hit.scenario
        if sc == C.ESCALATE:
            st.escalated = f"escalate:{hit.matched}"
            return Decision("escalate", sc, reason=hit.matched,
                            notes=[f"ESCALATE to POC now, do not reply. Trigger: {hit.matched!r}"])
        d = Decision("reply", sc)
        if sc == C.CHARGEBACK:
            self._say(d, st, "chargeback")
            st.escalated = "chargeback"
            d.notes.append("Chargeback/refund dispute: one firm reply sent, now escalated to POC.")
            return self._finish(d, st, now)
        if C.names_hard_no(text, self.p):
            self._say(d, st, "hard_no")
            d.notes.append("Fan asked for a hard-NO content type; refused.")
            return self._finish(d, st, now)
        if sc == C.IDENTITY:
            self._say(d, st, "identity")
            d.notes.append("Identity question answered with the honest line; review the thread.")
            return self._finish(d, st, now)
        if sc == C.BOUNDARY_PUSH:
            st.boundary_pushes += 1
            if st.boundary_pushes >= 3:
                st.escalated = "boundary_pushing"
                return Decision("escalate", sc, reason="repeated boundary pushing",
                                notes=["Fan pushed a boundary 3 times: escalate to POC (possible coercion)."])
        if sc == C.TOO_MANY_SALES:
            st.sales_paused_until_turn = st.turns + self.p.sales_cooldown_turns
            st.invite = None
        if sc == C.PAST_PROMISE:
            d.notes.append("Fan claims a past promise: check records before any commitment.")
        if sc == C.CUSTOM:
            return self._finish(self._custom(d, st), st, now)
        if sc == C.YES and st.invite:
            return self._finish(self._accept_invite(d, st), st, now)
        if sc == C.NO and st.invite:
            st.declined.append(st.invite["id"])
            st.invite = None
            self._say(d, st, "decline_ack")
            return self._finish(d, st, now)
        if sc == C.RATING_ASK:
            return self._finish(self._offer_rating(d, st), st, now)
        if sc == C.CONTENT_ASK:
            return self._finish(self._content_ask(d, st, text), st, now)
        # playbook scenario or small talk
        self._say(d, st, sc if sc in self.book.templates else "small_talk")
        if sc not in _NO_SELL_AFTER and self._may_pitch(st):
            self._invite_next_vault(d, st)
        return self._finish(d, st, now)

    def on_purchase(self, st, offer_id: str, amount_cents: int) -> Decision:
        """A paid unlock/tip reported by the platform. Advances the vault; thanks the fan."""
        st.spend_cents += max(0, int(amount_cents))
        if offer_id not in st.purchased:
            st.purchased.append(offer_id)
        if st.open_offer and st.open_offer.get("id") == offer_id:
            st.open_offer = None
        d = Decision("reply", "purchase")
        step = self._next_vault(st)
        if step and step[1].ppv_id == offer_id:
            st.vault_step += 1
            level = self.p.vault[st.vault_level]
            if st.vault_step >= len(level.steps):
                st.vault_level += 1
                st.vault_step = 0
                if st.vault_level >= len(self.p.vault):
                    self._make_vip(st, d)
                    return d
                self._say(d, st, "level_complete")
                d.notes.append(f"Fan completed vault level '{level.name}'.")
                return d
        if self.p.vip_spend_cents and not st.vip and st.spend_cents >= self.p.vip_spend_cents:
            self._make_vip(st, d)
            return d
        self._say(d, st, "purchase_thanks")
        return d

    # ── gates ──────────────────────────────────────────────────────────────────────────────────────────────────
    def _gate(self, st, now) -> Decision | None:
        if st.escalated:
            return Decision("skip", "escalated", reason=st.escalated,
                            notes=["Thread is escalated: waiting for an operator to clear it."])
        labels = set(st.labels)
        quiet = labels & set(self.p.quiet_labels)
        if quiet:
            return Decision("skip", "label", reason=f"do-not-engage label {''.join(quiet)}")
        for label, windows in self.p.shift_skip_labels.items():
            if label in labels and any(_in_window(now, w) for w in windows):
                return Decision("skip", "label", reason=f"{label} not answered in this shift")
        for label, minutes in self.p.throttle_labels.items():
            if label in labels and st.last_reply_at and not st.open_offer:
                last = datetime.fromisoformat(st.last_reply_at)
                if now - last < timedelta(minutes=minutes):
                    return Decision("skip", "label", reason=f"{label} answered at most every {minutes} min")
        return None

    # ── selling ────────────────────────────────────────────────────────────────────────────────────────────────
    def _may_pitch(self, st) -> bool:
        if st.open_offer or st.invite or st.turns < st.sales_paused_until_turn:
            return False
        if st.last_offer_turn is None:
            return st.turns >= self.p.first_offer_after_turns
        return st.turns - st.last_offer_turn >= self.p.repeat_offer_after_turns

    def _next_vault(self, st):
        """(level, step) of the next vault PPV in order, or None when the vault is finished/absent."""
        if st.vault_level >= len(self.p.vault):
            return None
        level = self.p.vault[st.vault_level]
        return level, level.steps[st.vault_step]

    def _invite_next_vault(self, d, st) -> None:
        nxt = self._next_vault(st)
        if not nxt or nxt[1].ppv_id in st.declined:
            return
        level, step = nxt
        if step.price_cents is None:
            d.notes.append(f"Next vault step {step.ppv_id} ({level.name}) has no approved price: not offered.")
            return
        st.invite = {"kind": "vault", "id": step.ppv_id}
        self._say(d, st, "invite", item=step.label)
        st.last_offer_turn = st.turns

    def _accept_invite(self, d, st) -> Decision:
        inv, st.invite = st.invite, None
        if inv["kind"] == "vault":
            nxt = self._next_vault(st)
            if not nxt or nxt[1].ppv_id != inv["id"]:
                self._say(d, st, "small_talk")
                d.notes.append("Accepted invite no longer matches the next vault step; nothing sent.")
                return d
            return self._send_offer(d, st, "vault", nxt[1].ppv_id, nxt[1].label, nxt[1].price_cents, "vault_offer")
        item = self.p.price(inv["id"])
        if not item:
            return d
        return self._send_offer(d, st, "item", item.key, item.label, item.price_cents, "item_offer")

    def _content_ask(self, d, st, text) -> Decision:
        items = C.requested_items(text, self.p)
        if items:
            allowed = [i for i in items if (st.vip or not i.vip_only) and i.key not in st.purchased]
            if not allowed:
                self._say(d, st, "vip_only_redirect")
                d.notes.append(f"Fan asked for VIP-only/owned items {[i.key for i in items]}; redirected.")
                if self.p.vault:
                    self._invite_next_vault(d, st)
                return d
            item = allowed[0]
            return self._send_offer(d, st, "item", item.key, item.label, item.price_cents, "item_offer")
        nxt = self._next_vault(st)          # a general "what do you have" goes to the next scripted step
        if nxt and nxt[1].ppv_id not in st.purchased:
            return self._send_offer(d, st, "vault", nxt[1].ppv_id, nxt[1].label, nxt[1].price_cents, "vault_offer")
        self._say(d, st, "small_talk")
        d.notes.append("Content ask with no matching priced item and no vault step left.")
        return d

    def _offer_rating(self, d, st) -> Decision:
        r = self.p.rating
        if not r:
            self._say(d, st, "small_talk")
            d.notes.append("Rating asked; this creator has no rating offer configured.")
            return d
        return self._send_offer(d, st, "rating", r.key, r.label, r.price_cents, "rating_offer")

    def _send_offer(self, d, st, kind, oid, label, price_cents, template) -> Decision:
        if price_cents is None:
            self._say(d, st, "price_unavailable")
            d.notes.append(f"{kind} {oid} has no approved price: operator must price it. Nothing quoted.")
            return d
        if oid in st.purchased:
            self._say(d, st, "already_bought")
            return d
        self._say(d, st, template, item=label, price_cents=price_cents)
        d.offer = {"kind": kind, "id": oid, "label": label, "price_cents": price_cents}
        st.open_offer = {**d.offer, "turn": st.turns}
        st.last_offer_turn = st.turns
        return d

    def _custom(self, d, st) -> Decision:
        if self.p.customs_vip_only and not st.vip:
            self._say(d, st, "custom_vip_only")
            d.notes.append("Custom asked by a non-VIP: not discussed (VIP-only policy).")
            return d
        if self.p.customs_min_cents is None:
            self._say(d, st, "custom_request")
            d.notes.append("Custom request: no approved minimum, operator must handle.")
            return d
        self._say(d, st, "custom_quote", price_cents=self.p.customs_min_cents)
        if self.p.customs_need_approval:
            d.notes.append(f"Custom request: quoted the {money(self.p.customs_min_cents)} minimum; confirm with the "
                           "assigned AA/OM before accepting.")
        return d

    def _make_vip(self, st, d) -> None:
        st.vip = True
        st.vip_since = self.clock().date().isoformat()
        self._say(d, st, "vip_welcome")
        dt = self.clock()
        d.notes.append(f"New VIP: label the fan Name[VIP:{dt.month}/{dt.day}/{dt.year}] and ask where they are from.")

    # ── text ───────────────────────────────────────────────────────────────────────────────────────────────────
    def _say(self, d, st, key, *, item=None, price_cents=None) -> None:
        variants = self.book.variants(key)
        if not variants:
            d.notes.append(f"playbook has no '{key}' template")
            return
        seed = int(hashlib.sha256(f"{st.creator_id}:{st.fan_id}:{st.turns}:{key}".encode()).hexdigest()[:8], 16)
        order = sorted(range(len(variants)), key=lambda i: (f"{key}#{i}" in st.recent_templates, (i + seed) % len(variants)))
        for i in order:
            try:
                text = render(variants[i], self.p, seed=seed, item=item, price_cents=price_cents)
            except MissingFact:
                continue
            if violates_never_say(text, self.p):
                continue
            if self.rephrase:
                try:
                    alt = self.rephrase(text, self.p)
                    if alt and alt.strip() and not violates_never_say(alt, self.p) and _same_numbers(text, alt):
                        text = alt.strip()
                except Exception:
                    pass
            d.messages.append(text)
            st.recent_templates = (st.recent_templates + [f"{key}#{i}"])[-12:]
            return
        d.notes.append(f"no usable '{key}' template for this persona")

    def _finish(self, d, st, now) -> Decision:
        if d.messages:
            st.turns += 1
            st.last_reply_at = now.isoformat()
        return d


def _same_numbers(a: str, b: str) -> bool:
    """An AI rephrase may not change any price or count."""
    import re
    return sorted(re.findall(r"\d+(?:\.\d+)?", a)) == sorted(re.findall(r"\d+(?:\.\d+)?", b))


def _in_window(now: datetime, window: str) -> bool:
    try:
        a, b = (int(x) for x in str(window).split("-"))
    except ValueError:
        return False
    h = now.hour
    return a <= h < b if a <= b else (h >= a or h < b)
