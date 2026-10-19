(() => {
  "use strict";
  if (window.top !== window || location.origin !== "https://onlyfans.com") return;
  window.addEventListener("message", event => {
    if (event.source !== window || event.origin !== location.origin || event.data?.__chatterly_capture !== true) return;
    const payload = event.data.payload;
    if (!payload || JSON.stringify(payload).length > 24000) return;
    chrome.runtime.sendMessage({type: "capture", payload}).catch(() => {});
  });
})();
