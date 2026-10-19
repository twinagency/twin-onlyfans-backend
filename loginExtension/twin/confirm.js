"use strict";
const status = document.getElementById("status"), identity = document.getElementById("identity"), detail = document.getElementById("detail"), confirmButton = document.getElementById("confirm"), cancelButton = document.getElementById("cancel");
async function update(type = "status") {
  confirmButton.disabled = true;
  try {
    const v = await chrome.runtime.sendMessage({type});
    status.textContent = v.phase === "submitted" ? "Submitted to Twin for verification." : v.phase === "ready" ? "OnlyFans is signed in as:" : v.phase === "waiting" ? "Finish signing in on the OnlyFans tab. If needed, open your OnlyFans profile to complete the connection." : v.phase === "retry" ? "Submission could not be confirmed." : v.phase === "submitting" ? "Submitting your connection…" : v.error || "Start from Your platforms in your Twin creator dashboard.";
    identity.textContent = v.handle ? "@" + v.handle : "";
    detail.textContent = ["ready", "retry"].includes(v.phase) ? "Confirm this is the OnlyFans account you want linked to your signed-in Twin creator profile. If it is the wrong account, cancel and sign in to the correct account on OnlyFans." : "";
    confirmButton.hidden = !["ready", "retry"].includes(v.phase); confirmButton.textContent = v.phase === "retry" ? "Retry the same connection" : "Connect this account";
    cancelButton.hidden = ["idle", "submitted"].includes(v.phase) || !v.phase;
  } catch (_) { status.textContent = "Reopen the Twin connector and try again."; }
  finally { confirmButton.disabled = false; }
}
confirmButton.addEventListener("click", () => update("confirm"));
cancelButton.addEventListener("click", () => update("cancel"));
update();
