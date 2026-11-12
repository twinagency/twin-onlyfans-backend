"""Log in with Browserbase: a remote browser the human signs in to themselves.

Ported (trimmed) from the private repo's remote_login.py. The relay creates a
Browserbase session (Browserbase's managed proxies, captcha solver OFF), drives
it over CDP (Playwright `connect_over_cdp`, no Chromium on this host), loads
OnlyFans and hands back Browserbase's LIVE VIEW. The human types their own
credentials / 2FA / challenge there: Fastt never sees a password.

Once OnlyFans sets `auth_id`, the login extension's interceptor
(loginExtension/interceptor.js, injected as an init script) has published the
signed headers + static_param; those plus the browser's cookies go through
`session_bootstrap.build_connection_capture_from_request` — the same
derivation POST .../capture-from-curl runs. The job writes nothing; the
dashboard runs its normal save + verify with the result.

Secrets: BROWSERBASE_API_KEY / BROWSERBASE_PROJECT_ID from env only. The API
key, the CDP connect URL and cookie values are scrubbed from every job error
and log line. The live-view URL is returned only while the job runs.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("of-relay.browserbase-login")

INTERCEPTOR_PATH = Path(__file__).resolve().parent.parent / "loginExtension" / "interceptor.js"

BROWSERBASE_API = "https://api.browserbase.com/v1"
BB_HTTP_TIMEOUT_S = 30
BB_VIEWPORT = {"width": 1280, "height": 800}

OF_HOME = "https://onlyfans.com/"
OF_CHATS = "https://onlyfans.com/my/chats/"
NAV_TIMEOUT_MS = 45_000

JOB_WINDOW_S = 600        # overall timeout, human login included
CAPTURE_RESERVE_S = 60    # the login must land this long before the window ends
CAPTURE_WAIT_S = 25       # after login: wait for a signed request + static_param
POLL_S = 1.0
JOB_TTL_S = 600           # a finished job stays readable this long
MAX_ACTIVE_JOBS = 4       # paid remote browsers: bound them per relay
EXPIRY_GRACE_S = 60       # a job still running this long past its window is hung: reaped
ERROR_MAX = 300

STARTING, WAITING_LOGIN, CAPTURING = "starting", "waiting_login", "capturing"
DONE, FAILED, CANCELLED, EXPIRED = "done", "failed", "cancelled", "expired"
STATES = (STARTING, WAITING_LOGIN, CAPTURING, DONE, FAILED, CANCELLED, EXPIRED)
_TERMINAL = {DONE, FAILED, CANCELLED, EXPIRED}
TIMED_OUT = "The login did not complete within 10 minutes."

# Stores the interceptor's postMessage payloads on window.__relayCapture,
# merging rules the way the extension's bridge.js does (static_param arrives on
# a later publish than the headers).
_CAPTURE_LISTENER_JS = r"""
(function () {
  window.addEventListener("message", function (e) {
    try {
      if (e.source !== window || !e.data || e.data.__chatterly_capture !== true) return;
      var p = e.data.payload || {}, prev = window.__relayCapture || {};
      var pr = prev.rules || {}, nr = p.rules || {};
      window.__relayCapture = {
        url: p.url, headers: p.headers || {},
        rules: {
          static_param: nr.static_param || pr.static_param || null,
          start: nr.start || pr.start || null,
          end: nr.end || pr.end || null,
        },
      };
    } catch (_) {}
  });
})();
"""


class Unconfigured(RuntimeError):
    pass


class Busy(RuntimeError):
    pass


class _Cancelled(Exception):
    pass


class _Expired(Exception):
    pass


def _clean(text: str) -> str:
    """One printable-ASCII line, at most ERROR_MAX chars."""
    return re.sub(r"\s+", " ", re.sub(r"[^\x20-\x7e]", " ", str(text))).strip()[:ERROR_MAX]


def scrub(text: Any, *secrets: str | None) -> str:
    """`text` with every non-empty secret replaced by ***, longest first."""
    s = "" if text is None else str(text)
    for secret in sorted({x for x in secrets if x}, key=len, reverse=True):
        s = s.replace(secret, "***")
    return s


# ── Job ─────────────────────────────────────────────────────────────────────

@dataclass
class Job:
    id: str
    tenant: str
    account: str
    created_at: float
    expires_at: float
    proxy: str | None = field(repr=False)  # saved connection proxy for the asset fetches (W3DC-01)
    state: str = STARTING
    error: str | None = None
    live_view_url: str | None = None
    session_id: str | None = None
    result: dict | None = field(default=None, repr=False)
    finished_at: float | None = None
    _secrets: set = field(default_factory=set, repr=False)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set_state(self, state: str, error: str | None = None) -> None:
        with self._lock:
            if self.state in _TERMINAL:
                return
            self.state = state
            if error:
                self.error = _clean(error)
            if state in _TERMINAL:
                self.finished_at = time.time()

    def is_terminal(self) -> bool:
        return self.state in _TERMINAL

    def public(self) -> dict[str, Any]:
        with self._lock:
            body = {"jobId": self.id, "state": self.state,
                    "liveViewUrl": None if self.state in _TERMINAL else self.live_view_url}
            if self.error:
                body["error"] = self.error
            if self.state == DONE and self.result:
                body.update(capture=self.result["capture"], xHash=self.result["xHash"],
                            ownerId=self.result["ownerId"])
            return body


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _gc_locked() -> list[Job]:
    """Expire hung jobs (still running past their window + grace: a Playwright
    call never returned) and drop finished ones past JOB_TTL_S. Returns the
    jobs just expired; the caller releases their sessions outside the lock."""
    now = time.time()
    reaped = [j for j in _jobs.values()
              if not j.is_terminal() and now > j.expires_at + EXPIRY_GRACE_S]
    for j in reaped:
        j._cancel.set()
        j.set_state(EXPIRED, TIMED_OUT)
    for jid in [jid for jid, j in _jobs.items()
                if j.is_terminal() and now - (j.finished_at or now) > JOB_TTL_S]:
        _jobs.pop(jid, None)
    return reaped


def _release_reaped(reaped: list[Job]) -> None:
    for j in reaped:  # off the request path: a release is an HTTP call
        try:
            threading.Thread(target=_release_job, args=(j,), daemon=True).start()
        except Exception:  # noqa: BLE001 — the session times out on its own
            pass


def _release_job(job: Job) -> None:
    """Release the job's Browserbase session once (worker or reaper, whichever first)."""
    with job._lock:
        sid, job.session_id = job.session_id, None
    _bb_release(sid)


def get_job(job_id: str) -> Job | None:
    with _jobs_lock:
        reaped = _gc_locked()
        job = _jobs.get(job_id)
    _release_reaped(reaped)
    return job


def find_job(tenant: str, account: str, job_id: str) -> Job | None:
    """The job, only when it belongs to this tenant + account."""
    job = get_job(job_id)
    return job if job is not None and job.tenant == tenant and job.account == account else None


def configured() -> bool:
    return bool(os.environ.get("BROWSERBASE_API_KEY") and os.environ.get("BROWSERBASE_PROJECT_ID"))


def start(tenant: str, account: str, *, proxy: str | None) -> Job:
    """The active job for (tenant, account), else a new one on a daemon thread."""
    if not configured():
        raise Unconfigured()
    with _jobs_lock:
        reaped = _gc_locked()
        active = [j for j in _jobs.values() if not j.is_terminal()]
        existing = next((j for j in active if j.tenant == tenant and j.account == account), None)
        busy = existing is None and len(active) >= MAX_ACTIVE_JOBS
        if existing is None and not busy:
            now = time.time()
            existing = Job(id=uuid.uuid4().hex, tenant=tenant, account=account,
                           created_at=now, expires_at=now + JOB_WINDOW_S, proxy=proxy)
            _jobs[existing.id] = existing
            fresh = True
        else:
            fresh = False
    _release_reaped(reaped)
    if busy:
        raise Busy()
    if fresh:
        try:
            _start_thread(existing)
        except Exception as e:  # noqa: BLE001
            log.warning("browserbase login %s could not start: %s", existing.id, type(e).__name__)
            existing.set_state(FAILED, "The login could not be started on the relay. Try again.")
            _release_job(existing)
    return existing


def _start_thread(job: Job) -> None:  # tests replace this
    threading.Thread(target=_run, args=(job,), name=f"bb-login-{job.id[:8]}", daemon=True).start()


def cancel(job: Job) -> str:
    job._cancel.set()
    job.set_state(CANCELLED)
    return job.state


# ── Worker ──────────────────────────────────────────────────────────────────

def _sync_playwright():  # tests replace this
    from playwright.sync_api import sync_playwright
    return sync_playwright()


def _run(job: Job) -> None:
    try:
        session = _bb_create_session(job)
        job._secrets.add(session["connectUrl"])
        live = _bb_live_view_url(session["id"])
        if not live:
            raise RuntimeError("Browserbase returned no live view for the session.")
        with job._lock:
            job.live_view_url = live
        with _sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(session["connectUrl"])
            try:
                context = browser.contexts[0]
                context.add_init_script(_CAPTURE_LISTENER_JS)
                context.add_init_script(INTERCEPTOR_PATH.read_text(encoding="utf-8"))
                page = context.pages[0] if context.pages else context.new_page()
                _check(job)
                try:
                    page.goto(OF_HOME, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                except Exception as e:  # noqa: BLE001
                    raise RuntimeError("Couldn't open OnlyFans in the remote browser: "
                                       + (str(e).splitlines() or [type(e).__name__])[0]) from None
                job.set_state(WAITING_LOGIN)
                _wait_for_login(job, page, context)
                job.set_state(CAPTURING)
                result = _capture(job, page, context)
                with job._lock:
                    if job.state not in _TERMINAL:
                        job.result = result
                job.set_state(DONE)
            finally:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
    except _Cancelled:
        job.set_state(CANCELLED)
    except _Expired:
        job.set_state(EXPIRED, TIMED_OUT)
    except Exception as e:  # noqa: BLE001
        msg = _clean(_scrub_job(job, f"{type(e).__name__}: {e}"))
        log.warning("browserbase login %s failed: %s", job.id, msg)
        job.set_state(FAILED, msg)
    finally:
        _release_job(job)
        if not job.is_terminal():
            job.set_state(FAILED, "The login ended without capturing a session.")
        job._secrets.clear()


def _scrub_job(job: Job, text: str) -> str:
    return scrub(text, os.environ.get("BROWSERBASE_API_KEY"), *job._secrets)


def _check(job: Job, reserve_s: float = 0) -> None:
    if job._cancel.is_set():
        raise _Cancelled()
    if time.time() > job.expires_at - reserve_s:
        raise _Expired()


def _wait(page, seconds: float) -> None:
    try:
        page.wait_for_timeout(int(seconds * 1000))
    except Exception:  # noqa: BLE001
        time.sleep(seconds)


def _auth_id(context) -> str | None:
    """auth_id is OnlyFans' logged-in cookie."""
    try:
        for c in context.cookies(["https://onlyfans.com"]):
            if c.get("name") == "auth_id" and c.get("value"):
                return c["value"]
    except Exception:  # noqa: BLE001
        pass
    return None


def _wait_for_login(job: Job, page, context) -> None:
    while True:
        _check(job, CAPTURE_RESERVE_S)
        if _auth_id(context):
            return
        _wait(page, POLL_S)


def _read_capture(page) -> dict[str, Any] | None:
    try:
        return page.evaluate("() => window.__relayCapture || null")
    except Exception:  # noqa: BLE001
        return None


_REQUIRED = ("user-id", "x-bc", "x-of-rev", "sign", "time")


def _wait_for_capture(job: Job, page) -> dict[str, Any]:
    """The interceptor's latest signed-request snapshot, waiting for its
    static_param (published on the first sign() call) up to CAPTURE_WAIT_S."""
    deadline = time.time() + CAPTURE_WAIT_S
    cap = None
    while time.time() < deadline:
        _check(job)
        cap = _read_capture(page)
        hdrs = (cap or {}).get("headers") or {}
        if all(hdrs.get(k) for k in _REQUIRED) and ((cap or {}).get("rules") or {}).get("static_param"):
            return cap
        _wait(page, POLL_S)
    hdrs = (cap or {}).get("headers") or {}
    if not all(hdrs.get(k) for k in _REQUIRED):
        raise RuntimeError("Logged in, but OnlyFans never fired a signed request. Try again.")
    return cap  # headers only: build falls back to a static_param seen at this rev


def _of_cookies(cookies: list[dict]) -> list[dict]:
    """Browser cookies for onlyfans.com at path /, in paste-curl's cookie shape."""
    out, seen = [], set()
    for c in cookies:
        name, value = c.get("name"), c.get("value")
        if (not name or not value or name in seen
                or (c.get("domain") or "").lstrip(".") != "onlyfans.com" or (c.get("path") or "/") != "/"):
            continue
        seen.add(name)
        out.append({"name": name, "value": value, "domain": ".onlyfans.com", "path": "/",
                    "expires": -1, "httpOnly": False, "secure": True, "sameSite": "None"})
    return out


def request_from_capture(cap: dict[str, Any], cookies: list[dict], user_agent: str) -> dict[str, Any]:
    """The capture in parse_curl's shape, with the x-relay-* headers the
    extension's Copy cURL adds."""
    h = cap.get("headers") or {}
    rules = cap.get("rules") or {}
    headers = {k: str(h[k]) for k in (*_REQUIRED, "x-hash") if h.get(k)}
    headers["user-agent"] = str(h.get("user-agent") or user_agent)
    for key, hdr in (("static_param", "x-relay-static-param"), ("start", "x-relay-sign-start"),
                     ("end", "x-relay-sign-end")):
        if rules.get(key):
            headers[hdr] = str(rules[key])
    return {"url": cap.get("url") or "https://onlyfans.com/api2/v2/users/me",
            "method": "GET", "headers": headers, "cookies": _of_cookies(cookies), "data": None}


def _capture(job: Job, page, context) -> dict:
    import session_bootstrap

    try:
        page.goto(OF_CHATS, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except Exception as e:  # noqa: BLE001 — a slow load; the interceptor may still have fired
        log.info("browserbase login %s: chats navigation did not settle: %s", job.id, type(e).__name__)
    cap = _wait_for_capture(job, page)
    cookies = context.cookies(["https://onlyfans.com"])
    job._secrets.update(str(c.get("value")) for c in cookies if c.get("value"))
    try:
        ua = page.evaluate("() => navigator.userAgent") or ""
    except Exception:  # noqa: BLE001
        ua = ""
    parsed = request_from_capture(cap, cookies, ua)
    try:
        return session_bootstrap.build_connection_capture_from_request(parsed, account=job.account, proxy=job.proxy)
    except session_bootstrap.CurlParseError as e:
        raise RuntimeError(f"Logged in, but the capture could not be read: {e}") from None


# ── Browserbase REST (stdlib; tests fake _bb_request) ───────────────────────

def _bb_env() -> tuple[str, str]:
    api_key = os.environ.get("BROWSERBASE_API_KEY")
    project_id = os.environ.get("BROWSERBASE_PROJECT_ID")
    if not api_key or not project_id:
        raise Unconfigured()
    return api_key, project_id


def _bb_session_payload(project_id: str) -> dict[str, Any]:
    """Browserbase's managed proxies, captcha solving OFF (it defaults on), a
    laptop viewport (readable live view), and a lifetime covering the window."""
    return {
        "projectId": project_id,
        "proxies": True,
        "timeout": JOB_WINDOW_S + 30,
        "browserSettings": {"solveCaptchas": False, "viewport": dict(BB_VIEWPORT)},
    }


def _bb_request(path: str, method: str = "GET", body: dict | None = None) -> dict[str, Any]:
    api_key, _ = _bb_env()
    req = urllib.request.Request(
        f"{BROWSERBASE_API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-BB-API-Key": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=BB_HTTP_TIMEOUT_S) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Browserbase {method} {path} failed (HTTP {e.code}): {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Browserbase {method} {path} failed: {e.reason}") from None
    return json.loads(raw.decode() or "{}")


def _bb_create_session(job: Job) -> dict[str, Any]:
    """Create the session; its id goes on the job first, so the worker's (or
    the reaper's) release covers a response that then fails validation."""
    _, project_id = _bb_env()
    try:
        session = _bb_request("/sessions", "POST", _bb_session_payload(project_id))
    except RuntimeError as e:
        raise RuntimeError(f"Couldn't start a Browserbase session: {e}") from None
    if session.get("id"):
        with job._lock:
            job.session_id = str(session["id"])
    if not session.get("connectUrl") or not session.get("id"):
        raise RuntimeError("Browserbase session-create returned no id/connectUrl")
    return session


def _bb_live_view_url(session_id: str) -> str | None:
    try:
        dbg = _bb_request(f"/sessions/{session_id}/debug")
    except Exception as e:  # noqa: BLE001
        log.warning("browserbase live view unavailable for %s: %s", session_id,
                    scrub(e, os.environ.get("BROWSERBASE_API_KEY")))
        return None
    url = dbg.get("debuggerFullscreenUrl") or dbg.get("debuggerUrl")
    pages = dbg.get("pages") or []
    if not url and pages:
        url = pages[0].get("debuggerFullscreenUrl") or pages[0].get("debuggerUrl")
    return url


def _bb_release(session_id: str | None) -> None:
    if not session_id:
        return
    try:
        _, project_id = _bb_env()
        _bb_request(f"/sessions/{session_id}", "POST",
                    {"projectId": project_id, "status": "REQUEST_RELEASE"})
    except Exception as e:  # noqa: BLE001 — the session times out on its own
        log.info("browserbase release of %s failed: %s", session_id,
                 scrub(e, os.environ.get("BROWSERBASE_API_KEY")))
