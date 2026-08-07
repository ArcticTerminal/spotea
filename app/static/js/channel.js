document.addEventListener("DOMContentLoaded", () => {
  const btn = document.getElementById("unfollow-channel-btn");
  if (!btn) return;

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    const success = await unfollowChannel(btn.dataset.feedId);
    if (success) {
      // This channel no longer exists — back to Library rather than
      // re-rendering a now-404 page.
      window.location.href = "/#library";
      return;
    }
    btn.disabled = false;
  });
});
