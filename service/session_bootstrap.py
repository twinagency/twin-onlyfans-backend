#!/usr/bin/env python3
"""
Multi-mode session bootstrap.

Three ways for the user to register a working session:

  A) "incogniton-default"
     Use the Incogniton profile id baked in connect_incogniton.py (env override
     INCOGNITON_PROFILE_ID). Just runs capture_session.py + extract_rules.py.

  B) "incogniton-custom"
     Caller passes a profile_id explicitly. Same as (A) but for users with
     multiple Incogniton profiles / different accounts.

  C) "paste-curl"
     Caller pastes a single `curl ...` command from browser DevTools
     ("Network" tab → right-click a /api2/v2/* request → "Copy as cURL").
     We parse:
       * cookies from the `-b '…'` flag
       * headers (user-id, x-bc, x-of-rev, user-agent, sign, time) from -H flags
       * the URL → builds a sample (sha1Hex from sign + Time + URL + user_id)
     Then we fetch 2313.js with the captured x-of-rev and derive
     checksum_indexes + checksum_constant via of_signer.derive_rules_from_chunk.
     Requires `static_param`, `start`, `end` to either be provided as form
     fields or be inheritable from an existing session at the same x-of-rev
     (we keep them per-rev because they're identical for every user at that
     OF version).

This module is import-safe; capture_session has the Playwright deps.
"""
from __future__ import annotations

import json
import re
import sys
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SESSIONS_DIR = HERE / "sessions"
sys.path.insert(0, str(HERE))

import accounts as account_registry  # noqa: E402
from of_signer import derive_rules_from_chunk, verify_rules  # noqa: E402
from capture_owner import MissingAuthId, OwnerMismatch, check_capture_owner  # noqa: E402,F401

CHUNK_URL_TPL = "https://static2.onlyfans.com/static/prod/f/{rev}/2313.js"


# ─── A + B: Incogniton-driven capture ─────────────────────────────────────

def run_incogniton(profile_id: str | None = None, *,
                   account_id: str | None = None,
                   nickname: str | None = None,
                   make_active: bool = True) -> Path:
    """Run capture_session.py + extract_rules.py and return the session path.

    The capture writes a session file into the OF account's per-account
    directory (`sessions/accounts/<user_id>/`) — bucketed by the user_id OF
    returns. The optional `account_id` arg is used only as a hint for the
    incogniton profile lookup; the real account_id is always derived from
    the captured session's `headers.user_id`.

    - `profile_id`: Incogniton profile UUID. If None, falls back to
      `connect_incogniton.PROFILE_ID` (env INCOGNITON_PROFILE_ID).
    - `account_id`: if set + has stored `incogniton_profile_id` in meta,
      and `profile_id` is None, reuse the stored one.
    - `nickname`: human label for the account; persisted to meta.json.
    - `make_active`: if True, mark this account active after the capture.
    """
    import os
    if not profile_id and account_id:
        meta = account_registry.get_account(account_id)
        if meta and meta.get("incogniton_profile_id"):
            profile_id = meta["incogniton_profile_id"]
    if profile_id:
        os.environ["INCOGNITON_PROFILE_ID"] = profile_id
    sys.modules.pop("connect_incogniton", None)
    sys.modules.pop("capture_session", None)
    import capture_session
    raw_path = capture_session.capture()
    import extract_rules
    rc = extract_rules.main(["extract_rules.py", str(raw_path)])
    if rc != 0:
        raise RuntimeError(f"extract_rules failed with code {rc}")
    # capture_session writes to the legacy flat dir; move it into the
    # right per-account dir based on user_id, then persist meta.
    final_path = _adopt_into_account(
        raw_path,
        nickname=nickname,
        incogniton_profile_id=profile_id,
        make_active=make_active,
    )
    return final_path


# ─── D: Playwright-with-proxy capture (no Incogniton) ─────────────────────

def run_playwright_proxy(proxy_label: str | None = None, *,
                         account_id: str | None = None,
                         nickname: str | None = None,
                         make_active: bool = True,
                         headless: bool = False) -> Path:
    """Capture a session through a fresh Playwright Chromium tunneled via
    a proxy. The user logs in once through that proxy IP, so the cookies are
    minted from the SAME egress the relay will later use for signed traffic
    — no teleportation when sending.

    Proxy selection (in order):
      1. `proxy_label` — explicit registry label.
      2. `account_id` — re-capture flow: look up whatever proxy is already
         bound to this account in the registry, reuse it.
    At least one must resolve to a registered proxy.

    After capture, the proxy is auto-assigned to the new account so the
    relay starts routing through it on the very next request.

    Other args mirror run_incogniton:
      - nickname / make_active: same semantics.
      - headless: default False (the user needs to log in interactively).
    """
    import proxies as proxy_registry
    p = None
    if proxy_label:
        p = proxy_registry.get_by_label(proxy_label)
        if p is None:
            raise ValueError(
                f"No proxy with label {proxy_label!r}. Add it under Setup → Proxies first."
            )
    elif account_id:
        p = proxy_registry.get_for_account(account_id)
        if p is None:
            raise ValueError(
                f"Account {account_id!r} has no proxy assigned yet. Either pass "
                f"`proxy_label` explicitly or assign one under Setup → Proxies first."
            )
        proxy_label = p["label"]
    else:
        raise ValueError("playwright-proxy mode requires either `proxy_label` or an `account_id` "
                         "that already has a proxy assigned.")
    proxy_url = proxy_registry.proxy_url(p)

    sys.modules.pop("capture_session_proxy", None)
    try:
        import capture_session_proxy
    except ImportError as e:
        raise RuntimeError(
            "playwright-proxy mode requires Playwright + Chromium installed locally. "
            "Run on the host (not in Docker):\n"
            "  ./venv/bin/pip install playwright\n"
            "  ./venv/bin/playwright install chromium"
        ) from e
    raw_path = capture_session_proxy.capture(
        proxy_url, proxy_label=proxy_label, headless=headless,
        # When re-capturing a known account, reuse its on-disk browser
        # profile so cookies + the captcha-solved state survive. For a
        # fresh capture without account_id, the capture module falls back
        # to bucketing by proxy_label.
        account_hint=account_id,
    )

    import extract_rules
    rc = extract_rules.main(["extract_rules.py", str(raw_path)])
    if rc != 0:
        raise RuntimeError(f"extract_rules failed with code {rc}")

    final_path = _adopt_into_account(
        raw_path,
        nickname=nickname,
        incogniton_profile_id=None,  # this mode bypasses Incogniton entirely
        make_active=make_active,
    )
    # Auto-bind the proxy to the freshly-captured account. The whole point of
    # this flow is "capture and runtime use the same egress", so leaving the
    # proxy unassigned afterward would defeat it.
    actual_aid = final_path.parent.name
    try:
        proxy_registry.assign_account(proxy_label, actual_aid)
        print(f"[bootstrap] Auto-assigned proxy {proxy_label!r} → account {actual_aid}")
    except KeyError:
        # Shouldn't happen — we already checked get_by_label above — but
        # don't crash the whole bootstrap if registry mutated mid-flight.
        print(f"[bootstrap] WARNING: proxy {proxy_label!r} disappeared mid-bootstrap; "
              f"assign manually under Setup → Proxies.")
    return final_path


def _adopt_into_account(raw_path: Path, *,
                        nickname: str | None,
                        incogniton_profile_id: str | None,
                        make_active: bool) -> Path:
    """Move a session file (and its chunk) into the per-account dir,
    update meta.json, and optionally mark the account active."""
    data = json.loads(raw_path.read_text())
    uid = str((data.get("headers") or {}).get("user_id") or "")
    if not uid:
        raise RuntimeError("Captured session is missing headers.user_id — cannot route to account")
    adir = account_registry.account_dir(uid)
    adir.mkdir(parents=True, exist_ok=True)

    # Move session JSON
    target = adir / raw_path.name
    if raw_path.resolve() != target.resolve():
        if target.exists():
            target.unlink()
        raw_path.rename(target)

    # Move chunk_2313_<ts>.js if present alongside in the legacy dir
    chunk_name = (data.get("signing") or {}).get("chunk_2313_file")
    if chunk_name:
        src_chunk = SESSIONS_DIR / chunk_name
        if src_chunk.exists():
            dst_chunk = adir / chunk_name
            if not dst_chunk.exists():
                src_chunk.rename(dst_chunk)

    account_registry.record_session(uid, target)
    account_registry.upsert_account(
        uid, nickname=nickname,
        incogniton_profile_id=incogniton_profile_id,
    )
    if make_active:
        account_registry.set_active_account_id(uid)
    print(f"[bootstrap] Adopted session into account {uid} ({target.relative_to(SESSIONS_DIR.parent)})")
    return target


# ─── C: paste-curl ───────────────────────────────────────────────────────

class CurlParseError(ValueError):
    pass


def _check_owner(account: str, user_id: str, cookies: list) -> None:
    try:
        check_capture_owner(account, user_id, cookies)
    except MissingAuthId as e:
        raise CurlParseError(str(e)) from None


def _sign_sample(sign: str, start_hint: str | None, end_hint: str | None,
                 url: str, time_ms: str, user_id: str) -> tuple[str, str, dict]:
    """Split Sign "start:sha1:hexchecksum:end" into (start, end, verify sample).
    The extension's x-relay-sign-start/end are cross-checks: a mismatch means
    inconsistent page state. Errors never carry the captured values."""
    parts = sign.split(":")
    if len(parts) != 4:
        raise CurlParseError("Sign header malformed (need 4 colon parts)")
    start, sha1Hex, hexchecksum, end = parts
    for label, supplied, wire in (("start", start_hint, start), ("end", end_hint, end)):
        if supplied and supplied != wire:
            raise CurlParseError(f"x-relay-sign-{label} disagrees with the Sign header — page state is inconsistent")
    try:
        checksum_value = abs(int(hexchecksum, 16))
    except ValueError:
        raise CurlParseError("Sign checksum portion is not hex") from None
    return start, end, {"sha1Hex": sha1Hex, "checksumValue": checksum_value,
                        "url": url, "time": time_ms, "user_id": user_id}


def parse_curl(curl_text: str) -> dict:
    """Parse a `curl ...` command (the kind DevTools generates).

    Returns:
      {
        "url":      str,
        "method":   "GET"|"POST"|...,
        "headers":  {lowercase_name: value, ...},
        "cookies":  [{"name", "value", "domain", "path"}, ...],
        "data":     str | None,
      }
    """
    # Normalise line continuations + dollar-quoted strings DevTools sometimes adds
    text = curl_text.strip()
    # Bash line continuations
    text = text.replace("\\\n", " ")
    # `$'…'` quoting → strip the $
    text = re.sub(r"\$('[^']*')", r"\1", text)

    try:
        tokens = shlex.split(text)
    except ValueError as e:
        raise CurlParseError(f"shell parse failed: {e}") from e

    if not tokens or tokens[0] != "curl":
        raise CurlParseError("expected the command to start with `curl`")

    url: str | None = None
    method = "GET"
    headers: dict[str, str] = {}
    cookies_raw = ""
    data: str | None = None

    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t in ("-H", "--header"):
            i += 1
            kv = tokens[i]
            if ":" in kv:
                k, v = kv.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        elif t in ("-b", "--cookie"):
            i += 1
            cookies_raw = tokens[i]
        elif t in ("-X", "--request"):
            i += 1
            method = tokens[i].upper()
        elif t in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii"):
            i += 1
            data = tokens[i]
            if method == "GET":
                method = "POST"
        elif t.startswith(("--compressed", "--insecure", "-k", "--http2", "--http1.1")):
            pass
        elif t in ("-A", "--user-agent"):
            i += 1
            headers["user-agent"] = tokens[i]
        elif t in ("-e", "--referer"):
            i += 1
            headers["referer"] = tokens[i]
        elif t.startswith("-"):
            # Skip unknown flags + their possible arg. Best-effort.
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-") and not tokens[i + 1].startswith("http"):
                i += 1
        elif url is None:
            url = t
        i += 1

    if not url:
        raise CurlParseError("no URL found in command")

    # Firefox "Copy as cURL" sends cookies as -H 'Cookie: ...'; -b wins if both.
    hdr_cookies = headers.pop("cookie", "")
    cookies_raw = cookies_raw or hdr_cookies
    cookies: list[dict] = []
    if cookies_raw:
        for pair in cookies_raw.split(";"):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            cookies.append({
                "name": name.strip(), "value": value.strip(),
                "domain": ".onlyfans.com", "path": "/",
                "expires": -1, "httpOnly": False, "secure": True, "sameSite": "None",
            })

    return {"url": url, "method": method, "headers": headers,
            "cookies": cookies, "data": data}


def from_curl(curl_text: str, *,
              static_param_override: str | None = None,
              account_id: str | None = None,
              nickname: str | None = None,
              make_active: bool = True) -> Path:
    """Build a complete session from a single DevTools curl command.

    The curl MUST be for a /api2/v2/* endpoint OF signs — it must have:
      -H 'user-id: ...', -H 'x-bc: ...', -H 'x-of-rev: ...',
      -H 'sign: start:sha1:hex:end', -H 'time: <ms>',
      -H 'user-agent: ...', -b 'cookies...'

    `static_param_override` is for users who know what static_param is for the
    OF revision they're pasting. If None, we look up by x-of-rev in any
    existing session (since static_param is per-OF-revision, identical for
    all users at that revision).
    """
    parsed = parse_curl(curl_text)
    h = parsed["headers"]
    required = ("user-id", "x-bc", "x-of-rev", "sign", "time", "user-agent")
    missing = [k for k in required if not h.get(k)]
    if missing:
        raise CurlParseError(
            f"curl is missing required signed-request headers: {missing}. "
            f"Make sure the curl was copied from a /api2/v2/* request, not a "
            f"static asset GET."
        )

    user_id = h["user-id"]
    x_bc = h["x-bc"]
    x_of_rev = h["x-of-rev"]
    user_agent = h["user-agent"]
    sign = h["sign"]
    time_ms = h["time"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", x_of_rev):
        raise CurlParseError("x-of-rev header is not an OnlyFans revision")
    # The relay files a capture under its own user-id (rerouted below), so only
    # the auth_id cookie can disagree here.
    _check_owner(f"of-{user_id}", user_id, parsed["cookies"])

    # Runtime fields the loginExtension may inject as custom -H headers.
    # The wire never carries these — they're only knowable from a hooked
    # webpack module on the live page. Their presence is what lets a
    # brand-new x-of-rev succeed without first running an Incogniton
    # bootstrap to seed static_param.
    curl_static_param = h.get("x-relay-static-param")
    curl_sign_start   = h.get("x-relay-sign-start")
    curl_sign_end     = h.get("x-relay-sign-end")

    start, end, sample = _sign_sample(sign, curl_sign_start, curl_sign_end,
                                      parsed["url"], time_ms, user_id)

    # ── Get static_param ─────────────────────────────────────
    # Priority:
    #   1. Body-level static_param_override (the "advanced" textbox in the UI)
    #   2. Per-curl x-relay-static-param header from the loginExtension
    #      (the normal path — extension hooks js-sha1 on the live page)
    #   3. Cross-session lookup by x-of-rev (works once any account has
    #      already been captured at the current rev)
    static_param = static_param_override or curl_static_param
    if not static_param:
        static_param = _lookup_static_param_for_rev(x_of_rev)
    if not static_param:
        raise CurlParseError(
            f"No static_param available for x-of-rev {x_of_rev!r}.\n"
            f"Use the Fastt Login extension (loginExtension/) — sign in, "
            f"open a chat so OF's sign() fires once, then Copy curl. The "
            f"extension injects x-relay-static-param so the relay can derive "
            f"signing rules for this revision automatically.\n"
            f"(Manual fallback: paste static_param into the 'advanced' "
            f"override box, or run the Incogniton bootstrap once.)"
        )

    # ── Fetch 2313.js + derive checksum indexes/constant ─────
    # Through the account's proxies.json proxy (curl_cffi, capped, no redirects);
    # assigned-but-unusable refuses, never falls back to direct. None = no proxy assigned.
    import proxies as proxy_registry
    from onlyfans_runtime import browser_capture
    try:
        p = proxy_registry.get_for_account(account_id or user_id)
        proxy = proxy_registry.proxy_url(p) if p is not None else None
    except Exception:
        raise CurlParseError("assigned proxy for this account is unusable; fix it under Setup → Proxies") from None
    chunk_url = CHUNK_URL_TPL.format(rev=x_of_rev)
    print(f"[bootstrap] Fetching {chunk_url}")
    chunk_code = browser_capture.fetch_asset("rules", owner=user_id, rev=x_of_rev,
                                             user_agent=user_agent, proxy=proxy)
    print(f"[bootstrap] Got chunk ({len(chunk_code)} bytes)")

    derived = derive_rules_from_chunk(chunk_code, [sample])
    if not derived:
        raise RuntimeError(
            "Could not derive checksum_indexes/constant from chunk. OF may "
            "have changed obfuscation; update _NUM5_* regexes in of_signer.py."
        )

    rules = {
        "static_param": static_param,
        "start": start,
        "end": end,
        "checksum_indexes": derived["checksum_indexes"],
        "checksum_constant": derived["checksum_constant"],
    }
    # Sanity: verify rules against the very sample we built
    ok, _ = verify_rules(rules, [sample])
    if not ok:
        raise RuntimeError(
            "Derived rules failed to reproduce the captured Sign. The "
            "static_param value is likely wrong for this OF revision."
        )
    print("[bootstrap] Rules verified against the captured sample.")

    # ── Persist session into the right account dir ───────────
    adir = account_registry.account_dir(user_id)
    adir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    chunk_file = f"chunk_2313_{ts}.js"
    (adir / chunk_file).write_text(chunk_code)

    dump = {
        "captured_at": ts,
        "profile_id": "paste-curl",
        "page_url": parsed["url"],
        "headers": {
            "user_id": user_id, "x_bc": x_bc,
            "x_of_rev": x_of_rev, "user_agent": user_agent,
        },
        "signing": {
            "rules": rules,
            "samples": [sample],
            "last_sign_seen": sign,
            "last_time_seen": int(time_ms),
            "chunk_2313_url": chunk_url,
            "chunk_2313_file": chunk_file,
        },
        "cookies": parsed["cookies"],
        "local_storage": {},
    }
    session_path = adir / f"session_{ts}.json"
    session_path.write_text(json.dumps(dump, indent=2))
    account_registry.record_session(user_id, session_path)
    # account_id arg is treated as a hint only — the real binding is by user_id
    # (OF's identity), so pasting a different user's curl never silently
    # clobbers another account. We just persist nickname / activate if asked.
    account_registry.upsert_account(user_id, nickname=nickname)
    if make_active:
        account_registry.set_active_account_id(user_id)
    if account_id and account_id != user_id:
        print(f"[bootstrap] Note: requested account_id={account_id!r} but "
              f"the pasted curl belongs to user_id={user_id!r} — routed to "
              f"the correct account.")
    print(f"[bootstrap] Saved {session_path.relative_to(SESSIONS_DIR.parent)}")
    return session_path


def build_connection_capture(curl_text: str, *,
                             static_param_override: str | None = None,
                             account: str, proxy: str | None) -> dict:
    """Derive a dashboard connection capture from a single DevTools curl.

    Pure derivation twin of `from_curl` — parses the curl, resolves
    static_param, fetches 2313.js, derives + verifies the signing rules, and
    returns exactly the shape the dashboard connection contract expects
    (`connectionCapture` in twin-dashboard/lib/onlyfans-connections/contract.ts):

        {"capture": {headers, signing.rules, cookies}, "xHash": ..., "ownerId": ...}

    It writes nothing and touches no account registry — the caller (the
    connection API) only needs the material to run its own save + verify.
    The hard obfuscation-sensitive step stays in of_signer.derive_rules_from_chunk,
    the single source shared with from_curl.
    """
    return build_connection_capture_from_request(
        parse_curl(curl_text), static_param_override=static_param_override, account=account, proxy=proxy)


def build_connection_capture_from_request(parsed: dict, *,
                                          static_param_override: str | None = None,
                                          account: str, proxy: str | None) -> dict:
    """`build_connection_capture` on an already-parsed request (parse_curl's
    shape) — the Browserbase login feeds its browser capture in here.

    The capture must belong to one OnlyFans login: `user-id` == `auth_id`
    cookie, and == the owner of `account` (`of-<owner>`). OnlyFans assets are
    fetched with curl_cffi through `proxy` (required: the saved connection's
    proxy, or None when none is saved), never urllib/env proxies."""
    from onlyfans_runtime.browser_capture import fetch_asset
    h = parsed["headers"]
    required = ("user-id", "x-bc", "x-of-rev", "sign", "time", "user-agent")
    missing = [k for k in required if not h.get(k)]
    if missing:
        raise CurlParseError(
            f"curl is missing required signed-request headers: {missing}. "
            f"Copy a /api2/v2/* request, not a static asset GET."
        )
    user_id, x_bc, x_of_rev = h["user-id"], h["x-bc"], h["x-of-rev"]
    user_agent, sign, time_ms = h["user-agent"], h["sign"], h["time"]
    if not re.fullmatch(r"[1-9][0-9]{0,18}", user_id):
        raise CurlParseError("user-id header is not a numeric OnlyFans id")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", x_of_rev):
        raise CurlParseError("x-of-rev header is not an OnlyFans revision")
    _check_owner(account, user_id, parsed["cookies"])
    asset = dict(owner=user_id, rev=x_of_rev, user_agent=user_agent, proxy=proxy)
    # The Fastt Login extension's curl carries no x-hash; it is a per-user
    # value OnlyFans serves publicly, so fetch it when the paste lacks one.
    x_hash = h.get("x-hash")
    if not x_hash:
        x_hash = fetch_asset("hash", **asset).strip()
        if not re.fullmatch(r"[\x21-\x7e]{8,512}", x_hash):
            raise RuntimeError("OnlyFans returned no usable X-Hash for this account.")
    start, end, sample = _sign_sample(sign, h.get("x-relay-sign-start"), h.get("x-relay-sign-end"),
                                      parsed["url"], time_ms, user_id)
    static_param = static_param_override or h.get("x-relay-static-param") or _lookup_static_param_for_rev(x_of_rev)
    if not static_param:
        raise CurlParseError(
            f"No static_param available for x-of-rev {x_of_rev!r}. Use the Fastt "
            f"Login extension (it injects x-relay-static-param), or paste a "
            f"static_param override."
        )
    chunk_code = fetch_asset("rules", **asset)
    derived = derive_rules_from_chunk(chunk_code, [sample])
    if not derived:
        raise RuntimeError("Could not derive checksum rules from chunk; update of_signer regexes.")
    rules = {
        "static_param": static_param, "start": start, "end": end,
        "checksum_indexes": derived["checksum_indexes"],
        "checksum_constant": derived["checksum_constant"],
    }
    ok, _ = verify_rules(rules, [sample])
    if not ok:
        raise RuntimeError("Derived rules failed to reproduce the captured Sign (static_param likely wrong for this revision).")
    capture = {
        "headers": {"user_id": user_id, "x_bc": x_bc,
                    "x_of_rev": x_of_rev, "user_agent": user_agent},
        "signing": {"rules": rules},
        "cookies": parsed["cookies"],
    }
    return {"capture": capture, "xHash": x_hash, "ownerId": int(user_id)}


# ─── helpers ─────────────────────────────────────────────────────────────

def _lookup_static_param_for_rev(x_of_rev: str) -> str | None:
    """Scan existing session files (across every account dir); return the
    first static_param that came from this OF revision. static_param is
    per-revision and identical across all users, so any account that has
    captured at this revision lets the paste-curl flow reuse the constant.
    """
    if not SESSIONS_DIR.is_dir():
        return None
    candidates: list[Path] = []
    # Per-account dirs
    accounts_dir = SESSIONS_DIR / "accounts"
    if accounts_dir.is_dir():
        for adir in accounts_dir.iterdir():
            if adir.is_dir():
                candidates.extend(adir.glob("session_*.json"))
    # Legacy flat dir (pre-migration, in case)
    candidates.extend(SESSIONS_DIR.glob("session_*.json"))
    for f in candidates:
        try:
            s = json.loads(f.read_text())
            if s.get("headers", {}).get("x_of_rev") != x_of_rev:
                continue
            sp = s.get("signing", {}).get("rules", {}).get("static_param")
            if sp:
                return sp
        except Exception:
            continue
    return None


# ─── CLI ─────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    mode = argv[1]
    if mode == "incogniton-default":
        run_incogniton()
        return 0
    if mode == "incogniton-custom":
        run_incogniton(argv[2])
        return 0
    if mode == "paste-curl":
        # Read curl from stdin
        curl = sys.stdin.read()
        from_curl(curl)
        return 0
    print(f"Unknown mode: {mode}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
