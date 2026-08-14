// Entry point for channel.html — one channel's track list.

import { setupSaveButtons, unfollowChannel } from "../content-actions.js";
import { installBfcacheReload, registerServiceWorker } from "../resume.js";

installBfcacheReload();
registerServiceWorker();
setupSaveButtons();

const btn = document.getElementById("unfollow-channel-btn");
btn?.addEventListener("click", async () => {
  btn.disabled = true;
  if (await unfollowChannel(btn.dataset.feedId)) {
    // This channel's page no longer means anything — back to Library rather
    // than re-rendering a now-404 page.
    window.location.href = "/#library";
    return;
  }
  btn.disabled = false;
});
