"use strict";
const ORIGIN = "https://dashboard.tgaiclone.xyz", OF = "https://onlyfans.com";
let queue = Promise.resolve();
const exactPage = url => { try { const u = new URL(url); return u.origin === ORIGIN && u.pathname === "/creator/platforms"; } catch (_) { return false; } };
const top = sender => sender.id === chrome.runtime.id && sender.frameId === 0;
const dashboard = sender => top(sender) && exactPage(sender.url) && Number.isInteger(sender.tab?.id);
const ownUi = sender => sender.id === chrome.runtime.id && sender.url === chrome.runtime.getURL("confirm.html");
const safe = s => typeof s === "string" && /^[\x20-\x7e]{1,4096}$/.test(s);
async function clear() { await chrome.storage.session.remove("job"); }
async function job() {
  const {job: value} = await chrome.storage.session.get("job");
  if (value && Date.now() >= value.deadline) { await clear(); return null; }
  return value || null;
}
const view = j => j ? {ok: true, job: j.id, phase: j.phase, handle: j.identity?.username || null, target: j.handle, error: j.error || null} : {ok: true, phase: "idle"};
async function currentPage(j) {
  try { const tab = await chrome.tabs.get(j.dashboardTab); return exactPage(tab.url); } catch (_) { return false; }
}
async function capture(m, sender, j) {
  if (!j || j.phase !== "waiting" || !top(sender) || sender.tab.id !== j.ofTab || new URL(sender.url).origin !== OF) return {ok: false};
  const p = m.payload, h = p?.headers, rules = p?.rules;
  if (!h || !rules || !["user-id", "user-agent", "x-bc", "x-of-rev", "sign", "time"].every(k => safe(h[k])) || !safe(rules.static_param)) return {ok: false};
  if (!/^[1-9][0-9]{0,15}$/.test(h["user-id"]) || !Number.isSafeInteger(Number(h["user-id"])) || !/^[A-Za-z0-9_-]{1,128}$/.test(h["x-of-rev"])) return {ok: false};
  const u = new URL(p.url, OF), parts = h.sign.split(":");
  if (u.origin !== OF || !u.pathname.startsWith("/api2/v2/") || u.href.length > 2048 || u.hash || !/^[0-9]{13}$/.test(h.time) || Math.abs(Date.now() - Number(h.time)) > 60000 ||
      parts.length !== 4 || !parts[0] || !parts[3] || !/^[a-f0-9]{40}$/.test(parts[1]) || !/^[a-f0-9]{1,8}$/.test(parts[2])) return {ok: false};
  const scope = [h["user-id"], h["x-of-rev"], h["x-bc"], rules.static_param].join("|");
  if (j.scope !== scope) { j.samples = []; j.identity = null; j.scope = scope; }
  if (!j.samples.some(s => s.sign.split(":")[1] === parts[1])) j.samples.push({url: u.href, sign: h.sign, time: h.time});
  j.samples = j.samples.slice(-4);
  if (p.identity?.id === Number(h["user-id"]) && typeof p.identity.username === "string" && /^[A-Za-z0-9_.-]{1,80}$/.test(p.identity.username)) j.identity = p.identity;
  if (j.samples.length >= 2 && j.identity && await currentPage(j)) {
    const stores = await chrome.cookies.getAllCookieStores();
    const store = stores.find(s => s.tabIds.includes(j.ofTab));
    if (!store) return {ok: false};
    const all = await chrome.cookies.getAll({url: OF + "/", storeId: store.id});
    const cookies = all.filter(c => ["onlyfans.com", ".onlyfans.com"].includes(c.domain) && c.path === "/" && !c.partitionKey)
      .map(({name, value, domain, path}) => ({name, value, domain, path}));
    if (cookies.find(c => c.name === "auth_id")?.value !== h["user-id"] || !cookies.length || cookies.length > 64 || new Set(cookies.map(c => c.name)).size !== cookies.length) return {ok: false};
    j.command = {profileId: j.profileId, handle: j.identity.username, session: {ownerId: Number(h["user-id"]), expiresAt: new Date(Date.now() + 6 * 3600000).toISOString(),
      browser: {schemaVersion: "of-browser.v1", headers: {user_id: h["user-id"], user_agent: h["user-agent"], x_bc: h["x-bc"], x_of_rev: h["x-of-rev"], x_hash: safe(h["x-hash"]) ? h["x-hash"] : null},
        cookies, staticParam: rules.static_param, samples: j.samples}}};
    if (JSON.stringify(j.command).length > 110000) { await clear(); return {ok: false}; }
    j.phase = "ready";
    await chrome.storage.session.set({job: j});
    await chrome.tabs.create({url: chrome.runtime.getURL("confirm.html")});
  } else await chrome.storage.session.set({job: j});
  return {ok: true};
}
async function handle(m, sender) {
  if (!m || typeof m.type !== "string") return {ok: false};
  if (m.type === "ping" && dashboard(sender)) return {ok: true, version: "1.0.0"};
  let j = await job();
  if (m.type === "start" && dashboard(sender)) {
    if (!/^[A-Za-z0-9._:-]{1,64}$/.test(m.profileId || "")) return {ok: false};
    if (j && !["submitted", "refused"].includes(j.phase)) {
      if (j.dashboardTab === sender.tab.id && j.documentId === sender.documentId) return view(j);
      if (j.dashboardTab === sender.tab.id) { await clear(); j = null; }
      else return {ok: false, error: "Finish or cancel the existing connection using the Twin Connect extension icon."};
    }
    j = {id: crypto.randomUUID(), dashboardTab: sender.tab.id, documentId: sender.documentId, ofTab: null, profileId: m.profileId,
      handle: null, deadline: Date.now() + 15 * 60000, phase: "waiting", samples: []};
    // A new tab binds capture to this attempt; existing sessions are never cleared.
    const tab = await chrome.tabs.create({url: "about:blank"});
    j.ofTab = tab.id; await chrome.storage.session.set({job: j});
    await chrome.tabs.update(tab.id, {url: OF + "/my/profile"}); return view(j);
  }
  if (m.type === "capture") return capture(m, sender, j);
  const fromDashboard = dashboard(sender) && j && j.dashboardTab === sender.tab.id && j.documentId === sender.documentId;
  if (m.type === "status") return fromDashboard || ownUi(sender) ? view(j) : {ok: true, phase: "idle"};
  if (m.type === "cancel" && (fromDashboard || ownUi(sender))) { await clear(); return {ok: true, phase: "idle"}; }
  if (m.type === "confirm" && ownUi(sender) && j && ["ready", "retry"].includes(j.phase)) {
    if (!await currentPage(j)) { await clear(); return {ok: false, error: "Reopen your Twin platforms page and start again."}; }
    j.phase = "submitting"; await chrome.storage.session.set({job: j});
    let result;
    try { result = await chrome.tabs.sendMessage(j.dashboardTab, {type: "submit", job: j.id, command: j.command}, {documentId: j.documentId}); }
    catch (_) { result = {ok: false, retry: true, error: "Return to the original Twin tab and check connection status before retrying."}; }
    j.phase = result?.ok ? "submitted" : result?.retry ? "retry" : "refused";
    j.error = result?.ok ? null : result?.error || "The connection could not be confirmed.";
    if (j.phase !== "retry") { delete j.command; delete j.samples; delete j.scope; }
    await chrome.storage.session.set({job: j});
    if (result?.ok) await chrome.tabs.update(j.dashboardTab, {active: true});
    return view(j);
  }
  return {ok: false, error: "Open connection setup from your Twin creator dashboard."};
}
chrome.runtime.onMessage.addListener((m, sender, respond) => {
  queue = queue.then(() => handle(m, sender)).then(respond, () => respond({ok: false, error: "Connection setup needs to be restarted."}));
  return true;
});
chrome.tabs.onRemoved.addListener(id => {
  queue = queue.then(async () => { const j = await job(); if (j && id === j.dashboardTab) await clear(); }).catch(() => {});
});
chrome.alarms.onAlarm.addListener(() => { queue = queue.then(job).catch(() => {}); });
chrome.alarms.create("expire-connection", {periodInMinutes: 1});
