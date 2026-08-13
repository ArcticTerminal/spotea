// Profile management. Only wired up on index.html — the drill-down pages
// (channel/player/content list) have no topbar and switching/managing
// profiles mid-drill-down would just orphan the page underneath, so they
// don't need this at all.
//
// Two separate overlays, deliberately not merged and not overlapping in
// what they do: #profiles-overlay ("Switch profile", from the topbar/mobile
// menu) is just a list you click to switch — no edit/delete/add, that's not
// what someone reaching for the header button wants mid-browse; it points
// to Settings for that. #profiles-manage-overlay ("Manage profiles", from
// Settings) is the reverse — rename (pencil — reuses the "New profile
// name…" field at the bottom instead of its own inline editor; see
// startEditing), delete (trash, with confirmDialog), and add, but no
// switching, so it doesn't double as a second way to do what the header
// button already does.

// Same classic "user" glyph everywhere (topbar button, profile rows) —
// plain white via currentColor, matching every other icon in the app
// (trash, export, refresh). Profiles aren't individually icon-customizable;
// this is just the fixed visual marker for "a profile".
const USER_ICON_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>';

let cachedProfiles = [];
// Set while the manage overlay's add/rename form is mid-rename (its "Add"
// button becomes "Save" and submitting calls renameProfile instead of
// createProfile) — null means it's in its normal "add a new profile" mode.
let editingProfileId = null;

async function fetchProfiles() {
  try {
    const res = await fetch("/profiles");
    if (!res.ok) return [];
    return await res.json();
  } catch (err) {
    return [];
  }
}

// Current profile gets the same accent border/tint treatment as a checked
// audio-quality option in Settings (see .profile-row.is-current in
// style.css) instead of a text badge.
function switchRowMarkup(profile) {
  return `
    <li class="profile-row${profile.is_current ? " is-current" : ""}" data-profile-id="${profile.id}">
      <button type="button" class="profile-row-main" data-profile-id="${profile.id}" ${profile.is_current ? "disabled" : ""}>
        <span class="profile-row-icon">${USER_ICON_SVG}</span>
        <span class="profile-row-name">${escapeHtml(profile.name)}</span>
      </button>
    </li>
  `;
}

// Manage-list rows: plain (non-clickable) name — this list is for
// rename/delete only, not switching, so there's no button/data-profile-id
// here to accidentally wire a click handler onto, and no is-current accent
// border either — that treatment marks "this is the one you'd switch away
// from," which doesn't mean anything on a list that can't switch.
function manageRowMarkup(profile) {
  return `
    <li class="profile-row" data-profile-id="${profile.id}">
      <div class="profile-row-info">
        <span class="profile-row-icon">${USER_ICON_SVG}</span>
        <span class="profile-row-name">${escapeHtml(profile.name)}</span>
      </div>
      <div class="profile-row-actions">
        <button type="button" class="btn-quiet-icon profile-rename" aria-label="Rename profile">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>
        </button>
        <button type="button" class="btn-quiet-icon profile-delete" aria-label="Delete profile">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path></svg>
        </button>
      </div>
    </li>
  `;
}

function renderSwitcherButton(profiles) {
  const current = profiles.find((p) => p.is_current);
  if (!current) return;

  const nameEl = document.getElementById("profile-switcher-name");
  if (nameEl) nameEl.textContent = current.name;

  // Same current-profile name, mirrored into the collapsed hamburger menu's
  // profile row (see the mobile-menu-btn breakpoint in style.css).
  const mobileNameEl = document.getElementById("mobile-menu-profile-name");
  if (mobileNameEl) mobileNameEl.textContent = current.name;
}

function renderProfileLists(profiles) {
  const switchList = document.getElementById("profiles-switch-list");
  if (switchList) switchList.innerHTML = profiles.map(switchRowMarkup).join("");

  const manageList = document.getElementById("profiles-manage-list");
  if (manageList) manageList.innerHTML = profiles.map(manageRowMarkup).join("");
}

async function loadProfiles() {
  cachedProfiles = await fetchProfiles();
  renderSwitcherButton(cachedProfiles);
  renderProfileLists(cachedProfiles);
}

async function switchProfile(profileId) {
  try {
    const res = await fetch(`/profiles/${profileId}/switch`, { method: "POST" });
    if (res.ok) {
      // Whatever's loaded belongs to the profile being left — its content id
      // means nothing under the new one. Without closing it first, the
      // reload below still fires ui.js's pagehide handler on the way out,
      // which would snapshot that now-foreign content id into
      // sessionStorage; the fresh load (already running as the new profile)
      // then tries to resume it, 404s against /content/{id}, and — since a
      // failed resume used to never clear its own stale record — repeats
      // that same failed attempt on every reload after, forever. closePlayer
      // (app.js) is always defined here: profiles.js only ever loads on
      // index.html, which always has the player overlay markup.
      closePlayer();
      window.location.reload();
      return;
    }
    showToast("Could not switch profile");
  } catch (err) {
    showToast("Could not switch profile");
  }
}

async function createProfile(name) {
  try {
    const res = await fetch("/profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (res.ok) {
      // The new profile auto-becomes active (see profiles.py) — a reload is
      // simplest, same "create and go" flow as adding a channel.
      window.location.reload();
      return;
    }
    const data = await res.json().catch(() => ({}));
    showToast(data.detail || "Could not create profile");
  } catch (err) {
    showToast("Could not create profile");
  }
}

// Returns whether the rename went through, so the caller can decide whether
// to leave the form in "editing" mode (e.g. on failure, so the typed name
// isn't lost) or reset it back to "add a new profile" mode.
async function renameProfile(profileId, name) {
  try {
    const res = await fetch(`/profiles/${profileId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (res.ok) {
      await loadProfiles();
      return true;
    }
    showToast("Could not rename profile");
  } catch (err) {
    showToast("Could not rename profile");
  }
  return false;
}

async function deleteProfile(profileId) {
  const confirmed = await confirmDialog(
    "Delete this profile? Its channels and downloads will be removed too.",
    "Delete"
  );
  if (!confirmed) return;

  try {
    const res = await fetch(`/profiles/${profileId}`, { method: "DELETE" });
    if (res.status === 204) {
      const wasCurrent = cachedProfiles.find((p) => p.id === profileId)?.is_current;
      // Deleting the profile you're currently viewing needs a full reload so
      // the page doesn't keep showing now-deleted content until the next
      // natural navigation — deleting some other profile just needs the
      // lists refreshed in place.
      if (wasCurrent) window.location.reload();
      else await loadProfiles();
      return;
    }
    const data = await res.json().catch(() => ({}));
    showToast(data.detail || "Could not delete profile");
  } catch (err) {
    showToast("Could not delete profile");
  }
}

// Sets up a simple overlay: one or more triggers open it, a close button /
// backdrop click / Escape close it. Returns the close function so a caller
// (setupManageOverlay) can layer extra behavior onto it.
function setupOverlay(overlayId, closeBtnId, triggerIds) {
  const overlay = document.getElementById(overlayId);
  if (!overlay) return null;
  const closeBtn = document.getElementById(closeBtnId);

  const open = () => {
    overlay.hidden = false;
  };
  const close = () => {
    overlay.hidden = true;
  };

  for (const triggerId of triggerIds) {
    document.getElementById(triggerId)?.addEventListener("click", open);
  }
  closeBtn?.addEventListener("click", close);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !overlay.hidden) close();
  });

  return close;
}

function setupSwitchOverlay() {
  setupOverlay("profiles-overlay", "profiles-close", ["profile-switcher-btn"]);
  // The collapsed mobile menu's own profile item opens this overlay
  // directly too — see setupMobileMenu in app.js.

  const switchList = document.getElementById("profiles-switch-list");
  switchList?.addEventListener("click", (event) => {
    const rowBtn = event.target.closest(".profile-row-main");
    if (!rowBtn || rowBtn.disabled) return;
    switchProfile(Number(rowBtn.dataset.profileId));
  });
}

function setupManageOverlay() {
  const baseClose = setupOverlay("profiles-manage-overlay", "profiles-manage-close", [
    "open-profiles-settings",
  ]);
  if (!baseClose) return;

  const manageList = document.getElementById("profiles-manage-list");
  const addForm = document.getElementById("profiles-add-form");
  const addInput = document.getElementById("profiles-add-input");
  const addSubmitBtn = addForm.querySelector('button[type="submit"]');

  function stopEditing() {
    editingProfileId = null;
    addInput.value = "";
    addSubmitBtn.textContent = "Add";
  }

  function startEditing(profile) {
    editingProfileId = profile.id;
    addInput.value = profile.name;
    addInput.focus();
    addInput.select();
    addSubmitBtn.textContent = "Save";
  }

  document.getElementById("profiles-manage-overlay").addEventListener("click", (event) => {
    if (event.target.id === "profiles-manage-overlay") stopEditing();
  });
  document.getElementById("profiles-manage-close").addEventListener("click", stopEditing);
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (document.getElementById("profiles-manage-overlay").hidden) return;
    // While mid-rename, the first Escape just backs out of that (so a typed
    // edit isn't lost to an accidental double-press); baseClose (already
    // wired to Escape by setupOverlay) handles the second press.
    if (editingProfileId !== null) stopEditing();
  });

  manageList.addEventListener("click", (event) => {
    const renameBtn = event.target.closest(".profile-rename");
    if (renameBtn) {
      const row = renameBtn.closest(".profile-row");
      const profile = cachedProfiles.find((p) => p.id === Number(row.dataset.profileId));
      if (profile) startEditing(profile);
      return;
    }

    const deleteBtn = event.target.closest(".profile-delete");
    if (deleteBtn) {
      const row = deleteBtn.closest(".profile-row");
      deleteProfile(Number(row.dataset.profileId));
    }
  });

  addForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = addInput.value.trim();
    if (!name) return;

    if (editingProfileId !== null) {
      if (await renameProfile(editingProfileId, name)) stopEditing();
    } else {
      createProfile(name);
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setupSwitchOverlay();
  setupManageOverlay();
  loadProfiles();
});
