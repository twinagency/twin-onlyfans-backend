(() => {
  "use strict";
  const origin = "https://dashboard.tgaiclone.xyz";
  const onPage = () => window.top === window && location.origin === origin && location.pathname === "/creator/platforms";
  if (!onPage()) return;
  let activeJob = null;
  const post = message => window.postMessage({source: "twin-connector", ...message}, origin);
  window.addEventListener("message", async event => {
    const m = event.data;
    if (!onPage() || event.source !== window || event.origin !== origin || m?.source !== "twin-dashboard" ||
        !["ping", "start", "status", "cancel"].includes(m.type) || typeof m.id !== "string" || !/^[a-f0-9-]{36}$/.test(m.id)) return;
    try {
      if (m.type === "start" && (typeof m.profileId !== "string" || m.profileId.length > 64)) return;
      const result = await chrome.runtime.sendMessage({type: m.type, profileId: m.profileId, handle: m.handle});
      if (m.type === "start" && result.ok) activeJob = result.job;
      if (m.type === "cancel") activeJob = null;
      post({id: m.id, ...result});
    } catch (_) { post({id: m.id, ok: false, error: "Reload this page after installing or updating Twin Connect."}); }
  });
  chrome.runtime.onMessage.addListener((m, sender, respond) => {
    if (sender.id !== chrome.runtime.id || !onPage() || m?.type !== "submit" || m.job !== activeJob) return;
    (async () => {
      try {
        const response = await fetch("/creator-auth/platforms/onlyfans/browser", {method: "POST", credentials: "same-origin", redirect: "error",
          headers: {"Content-Type": "application/json"}, body: JSON.stringify(m.command)});
        const body = await response.json();
        if (response.ok && body.ok === true && typeof body.enrollment?.id === "string") {
          activeJob = null; respond({ok: true});
        } else respond({ok: false, retry: response.status >= 500, error: "The connection could not be confirmed. Check your Twin dashboard before retrying."});
      } catch (_) { respond({ok: false, retry: true, error: "The connection could not be confirmed. Check your Twin dashboard before retrying."}); }
    })();
    return true;
  });
})();
