/**
 * V5 section 6 dashboard run. The ACTUAL dashboard component (components/onlyfans-preview/PlanCConsole.tsx @ 3cedc8f), rendered
 * with React in jsdom, using the ACTUAL browser client (lib/onlyfans/plan-c-client.ts) and the dashboard's ACTUAL server-side
 * handlers (plan-c-route.ts: planCOperatorOnly, planCConversationCommand, planCReconcile, planCReadUnresolved ...) and the ACTUAL
 * server-side backend transport (plan-c-backend.ts) over REAL HTTP to the Python backend app (onlyfans_app:app) on real PostgreSQL.
 *
 * Not exercised (needs Next + the dashboard session store; see DASHBOARD-FINDINGS.md): the Next route files' cookie/session
 * verification (requireOnlyFansRead / planCOwnerActor) and same-origin checks. Their output is modelled by the `identity` switch,
 * which feeds the real planCOperatorOnly() exactly the context the route builds.
 *
 * Protocol with the Python driver: after each step this prints one line `SYNC {json}` and waits for a line on stdin.
 */
import { createRequire } from "node:module";
import readline from "node:readline";
import React, { act } from "react";
const { JSDOM } = createRequire(import.meta.url)("jsdom");

const dom = new JSDOM('<div id="root"></div>', { url: "https://fixture.invalid/onlyfans/plan-c" });
Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, Event: dom.window.Event,
  MouseEvent: dom.window.MouseEvent, React, IS_REACT_ACT_ENVIRONMENT: true });

import { createPlanCBackend } from "../../lib/onlyfans/plan-c-backend";
import { parseCommandJson } from "../../lib/onlyfans/command-json";
import { PLAN_C_SCHEMA } from "../../lib/onlyfans/plan-c";
import { planCConversationCommand, planCOperatorOnly, planCReadDrafts, planCReadUnresolved, planCReconcile, type PlanCHttp } from "../../lib/onlyfans/plan-c-route";

const env = (name: string) => { const v = process.env[name]; if (!v) throw new Error(`missing ${name}`); return v; };
const API_BASE = env("API_BASE"), CONTROL_TOKEN = env("CONTROL_TOKEN"), ACCOUNT = env("ACCOUNT"), PILOT_ACTOR = env("PILOT_ACTOR"), OTHER_ACTOR = env("OTHER_ACTOR");
const EVIDENCE_TEXT = "provider history for the exact message was checked";

type Ident = { operatorSession: boolean; agency: boolean; viewAs: boolean; canManage: boolean; pilotActor: string | null };
const OPERATOR: Ident = { operatorSession: true, agency: false, viewAs: false, canManage: true, pilotActor: PILOT_ACTOR };
let ident: Ident = { ...OPERATOR };
let token = CONTROL_TOKEN;
let inflight = 0;
type Call = { method: string; path: string; status: number; backend: number | null; body?: unknown };
let calls: Call[] = [];
const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

/** The Plan C proxy routes as one fetch: the route files' gate (operator only) then the real handlers over real HTTP. */
const proxy = (async (input: RequestInfo | URL, init?: RequestInit) => {
  inflight++;
  const url = new URL(String(input), "https://fixture.invalid"), method = init?.method ?? "GET", path = decodeURIComponent(url.pathname);
  const body = init?.body ? parseCommandJson(String(init.body)) : undefined;
  const entry: Call = { method, path: path.replace(/^.*\/plan-c\//, ""), status: 0, backend: null, ...(body ? { body } : {}) };
  calls.push(entry);
  try {
    const send = (r: PlanCHttp) => { entry.status = r.status; return Response.json(r.body, { status: r.status }); };
    const gate = !ident.operatorSession
      ? { ok: false as const, status: 401, error: "Sign in as the operator to use Plan C approvals." }
      : planCOperatorOnly({ auth: { operatorSession: ident.operatorSession, agency: ident.agency, viewAs: ident.viewAs }, actor: ident.pilotActor, pilotActor: ident.pilotActor, canManage: ident.canManage });
    if (!gate.ok) return send({ status: gate.status, body: { schemaVersion: PLAN_C_SCHEMA, ok: false, outcome: "refused", error: gate.error, reload: false } });
    const actor = gate.actor;
    // Count the real backend statuses by wrapping fetch for this one call.
    const realFetch = globalThis.fetch;
    const backend = createPlanCBackend({ baseUrl: API_BASE, token, fetchImpl: (async (u: any, i: any) => { const r = await realFetch(u, i); entry.backend = r.status; return r; }) as typeof fetch });
    const m = /^\/api\/onlyfans-controls\/accounts\/([^/]+)\/plan-c\/(.*)$/.exec(path);
    if (!m) return new Response(null, { status: 404 });
    const accountId = m[1], rest = m[2];
    if (method === "GET" && rest === "drafts") return send(await planCReadDrafts(backend, accountId, actor));
    if (method === "GET" && rest === "unresolved") return send(await planCReadUnresolved(backend, accountId, actor));
    let x = /^conversations\/([^/]+)\/([^/]+)$/.exec(rest);
    if (method === "POST" && x) return send(await planCConversationCommand(backend, { actor, accountId, conversationId: x[1], action: x[2], body }));
    x = /^unresolved\/([^/]+)\/reconcile$/.exec(rest);
    if (method === "POST" && x) return send(await planCReconcile(backend, { actor, accountId, id: x[1], body }));
    return new Response(null, { status: 404 });
  } finally { inflight--; }
}) as typeof fetch;

// ---------------------------------------------------------------- DOM helpers
const all = (selector: string) => [...document.querySelectorAll<HTMLElement>(selector)];
const q = <T extends Element = HTMLElement>(selector: string, root: ParentNode = document) => root.querySelector<T>(selector);
let root: any, createRoot: any, PlanCConsole: any;
async function idle(ms = 150) { for (let i = 0; i < 400; i++) { await act(async () => { await sleep(ms); }); if (inflight === 0) { await act(async () => { await sleep(ms); }); if (inflight === 0) return; } } throw new Error("never idle"); }
async function mount(props: { canAct?: boolean } = {}) {
  if (root) await act(async () => root.unmount());
  document.body.innerHTML = '<div id="root"></div>';
  root = createRoot(document.getElementById("root")!);
  await act(async () => { root.render(<PlanCConsole accountId={ACCOUNT} canAct={props.canAct ?? true} authorizationMode="autonomous" fetchImpl={proxy} />); });
  await idle();
}
async function click(element: Element | null) {
  if (!element) throw new Error("element to click does not exist");
  await act(async () => { element.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true })); await sleep(30); });
}
const card = (conversationId: string) => q(`[data-plan-c-conversation="${conversationId}"]`);
async function conversationAction(conversationId: string, action: string, { confirm = true } = {}) {
  const c = card(conversationId); if (!c) return { present: false };
  const button = q<HTMLButtonElement>(`[data-plan-c-conversation-action="${action}"]`, c);
  if (!button || button.disabled) return { present: true, enabled: false };
  await click(button);
  if (confirm) { await click(q(`[data-plan-c-conversation-confirm="${action}"]`, card(conversationId)!)); await idle(); }
  return { present: true, enabled: true };
}
async function reload() { await click(q("[data-plan-c-reload]")); await idle(); }
function snapshot() {
  const snap = {
    unresolved: all("[data-plan-c-unresolved]").map(e => ({ id: e.getAttribute("data-plan-c-unresolved"), text: e.textContent, reconcile: !!q("[data-plan-c-reconcile]", e) })),
    conversations: all("[data-plan-c-conversation]").map(e => ({ id: e.getAttribute("data-plan-c-conversation"), text: e.textContent,
      actions: all("[data-plan-c-conversation]").length ? [...e.querySelectorAll<HTMLButtonElement>("[data-plan-c-conversation-action]")].map(b => ({ action: b.getAttribute("data-plan-c-conversation-action"), disabled: b.disabled })) : [],
      status: q('[role="status"]', e)?.textContent ?? null })),
    drafts: all("[data-plan-c-draft]").length,
    confirmation: q("[data-plan-c-confirmation]")?.textContent ?? null, notice: q("[data-plan-c-notice]")?.textContent ?? null,
    unresolvedError: q("[data-plan-c-unresolved-error]")?.textContent ?? null, draftsError: q("[data-plan-c-drafts-error]")?.textContent ?? null,
    noUnresolved: !!q("[data-plan-c-no-unresolved]"), calls };
  calls = [];
  return snap;
}

const rl = readline.createInterface({ input: process.stdin });
const lines: string[] = []; let waiter: ((line: string) => void) | null = null;
rl.on("line", line => { if (waiter) { const w = waiter; waiter = null; w(line); } else lines.push(line); });
const nextLine = () => new Promise<string>(resolve => { const queued = lines.shift(); if (queued !== undefined) resolve(queued); else waiter = resolve; });
async function sync(name: string, extra: Record<string, unknown> = {}) {
  console.log("SYNC " + JSON.stringify({ name, ...extra, snap: snapshot() }));
  const answer = await nextLine();
  if (answer.trim() !== "go") throw new Error("driver said " + answer);
}

async function main() {
  ({ createRoot } = await import("react-dom/client"));
  ({ default: PlanCConsole } = await import("../../components/onlyfans-preview/PlanCConsole"));
  const FAN = env("FAN_CONV"), FAN2 = env("FAN2_CONV");
  const scope = await createPlanCBackend({ baseUrl: API_BASE, token }).scope(ACCOUNT, PILOT_ACTOR);
  console.log("SCOPE " + JSON.stringify(scope));

  // 1. Held work is displayed with owner and next step; each held conversation is listed with its revision.
  await mount();
  await sync("display");

  // 2. Unauthorised and unauthenticated attempts. The console is first loaded as the operator, then the session/identity
  //    or the credential changes underneath it (an expired session, an agency or view-as session, a wrong token, another actor).
  const refused: Record<string, unknown>[] = [];
  const attempts: [string, () => void, () => void][] = [
    ["agency session", () => { ident = { ...OPERATOR, agency: true }; }, () => { ident = { ...OPERATOR }; }],
    ["view-as session", () => { ident = { ...OPERATOR, viewAs: true }; }, () => { ident = { ...OPERATOR }; }],
    ["no session", () => { ident = { ...OPERATOR, operatorSession: false }; }, () => { ident = { ...OPERATOR }; }],
    ["read-only operator (cannot manage)", () => { ident = { ...OPERATOR, canManage: false }; }, () => { ident = { ...OPERATOR }; }],
    ["wrong backend credential", () => { token = "not-the-control-token"; }, () => { token = CONTROL_TOKEN; }],
    ["another operator actor", () => { ident = { ...OPERATOR, pilotActor: OTHER_ACTOR }; }, () => { ident = { ...OPERATOR }; }],
  ];
  for (const [name, flip, restore] of attempts) {
    await mount(); snapshot();
    flip();
    const result = await conversationAction(FAN, "stop");
    refused.push({ name, result, snap: snapshot() });
    restore();
  }
  // A viewer without canAct sees the same list with every control disabled.
  await mount({ canAct: false });
  const viewer = snapshot();
  await sync("unauthorized", { refused, viewer });

  // 3. Authorised stop on FAN (fresh revision, two clicks).
  await mount(); snapshot();
  const stop = await conversationAction(FAN, "stop");
  await sync("stop", { stop });

  // 4. Authorised takeover on FAN2.
  await mount(); snapshot();
  const takeover = await conversationAction(FAN2, "takeover");
  await sync("takeover", { takeover });

  // 5. Stale revision: the driver changes FAN2 out of band (handback) while this screen still shows the takeover.
  await sync("stale-ready");
  const stale = await conversationAction(FAN2, "handback");
  await sync("stale", { stale });

  // 6. Authorised takeover then hand back on FAN2 with the fresh revision.
  await reload(); snapshot();
  const again = await conversationAction(FAN2, "takeover");
  await sync("takeover-2", { again });
  const back = await conversationAction(FAN2, "handback");
  await sync("handback", { back });

  // 7. Separate case resolution. The driver adds an independent provider-event hold for FAN2.
  await sync("event-ready");
  await reload();
  await sync("event-shown");
  // The unknown send for FAN accepts an evidence NOTE only; it never releases the hold.
  const row = q(`[data-plan-c-unresolved^="unknown_send:"]`);
  const input = row && q<HTMLInputElement>('input[aria-label="Evidence reference"]', row);
  if (input) {
    await act(async () => {
      Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value")!.set!.call(input, EVIDENCE_TEXT);
      input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    });
    await click(q("[data-plan-c-reconcile]", q(`[data-plan-c-unresolved^="unknown_send:"]`)!)); await idle();
  }
  await sync("evidence-note", { hadInput: !!input });
  await sync("resolved-elsewhere");
  await reload();
  await sync("final");
  if (root) await act(async () => root.unmount());
  dom.window.close();
  rl.close();
}
main().then(() => process.exit(0), error => { console.log("FAILED " + String(error?.stack ?? error)); process.exit(1); });
