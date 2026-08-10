// Profile switcher (topbar) + full profile management (Settings tab). Only
// wired up on index.html — the drill-down pages (channel/player/content
// list) have no topbar and switching profiles mid-drill-down would just
// orphan the page underneath, so they don't need this at all.

// Same classic "user" glyph everywhere (topbar button, switcher rows,
// settings rows) — plain white via currentColor, matching every other icon
// in the app (trash, export, refresh). Profiles aren't individually
// icon-customizable; this is just the fixed visual marker for "a profile".
const USER_ICON_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>';

let cachedProfiles = [];

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

// Settings rows: fixed icon + an always-editable name field (no separate
// view/edit toggle state — fewer moving parts) + delete.
function settingsRowMarkup(profile) {
  return `
    <li class="profile-row" data-profile-id="${profile.id}">
      <span class="profile-row-icon">${USER_ICON_SVG}</span>
      <form class="profile-edit-form">
        <input type="text" class="profile-name-input" maxlength="100" value="${escapeHtml(profile.name)}" aria-label="Name" required />
        <button type="submit" class="btn-quiet">Save</button>
        <button type="button" class="btn-quiet-icon profile-delete" aria-label="Delete profile">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path></svg>
        </button>
      </form>
    </li>
  `;
}

function renderSwitcherButton(profiles) {
  const current = profiles.find((p) => p.is_current);
  if (!current) return;

  const nameEl = document.getElementById("profile-switcher-name");
  if (nameEl) nameEl.textContent = current.name;

  // Same current-profile name, mirrored into the collapsed hamburger menu's
  // "Switch profile" row (see the mobile-menu-btn breakpoint in style.css).
  const mobileNameEl = document.getElementById("mobile-menu-profile-name");
  if (mobileNameEl) mobileNameEl.textContent = current.name;
}

function renderProfileLists(profiles) {
  const switchList = document.getElementById("profiles-switch-list");
  if (switchList) switchList.innerHTML = profiles.map(switchRowMarkup).join("");

  const settingsList = document.getElementById("profiles-settings-list");
  if (settingsList) settingsList.innerHTML = profiles.map(settingsRowMarkup).join("");
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

async function renameProfile(profileId, name) {
  try {
    const res = await fetch(`/profiles/${profileId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (res.ok) {
      await loadProfiles();
      return;
    }
    showToast("Could not rename profile");
  } catch (err) {
    showToast("Could not rename profile");
  }
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

function setupProfileSwitcher() {
  const btn = document.getElementById("profile-switcher-btn");
  const overlay = document.getElementById("profiles-overlay");
  const closeBtn = document.getElementById("profiles-close");
  const switchList = document.getElementById("profiles-switch-list");
  const addForm = document.getElementById("profiles-add-form");
  const addInput = document.getElementById("profiles-add-input");
  if (!btn || !overlay) return;

  btn.addEventListener("click", () => {
    overlay.hidden = false;
  });
  closeBtn.addEventListener("click", () => {
    overlay.hidden = true;
  });
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) overlay.hidden = true;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !overlay.hidden) overlay.hidden = true;
  });

  switchList.addEventListener("click", (event) => {
    const rowBtn = event.target.closest(".profile-row-main");
    if (!rowBtn || rowBtn.disabled) return;
    switchProfile(Number(rowBtn.dataset.profileId));
  });

  addForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const name = addInput.value.trim();
    if (!name) return;
    createProfile(name);
  });
}

function setupProfilesSettings() {
  const list = document.getElementById("profiles-settings-list");
  if (!list) return;

  list.addEventListener("submit", (event) => {
    const form = event.target.closest(".profile-edit-form");
    if (!form) return;
    event.preventDefault();

    const row = form.closest(".profile-row");
    const name = form.querySelector(".profile-name-input").value.trim();
    if (!name) return;
    renameProfile(Number(row.dataset.profileId), name);
  });

  list.addEventListener("click", (event) => {
    const deleteBtn = event.target.closest(".profile-delete");
    if (!deleteBtn) return;
    const row = deleteBtn.closest(".profile-row");
    deleteProfile(Number(row.dataset.profileId));
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setupProfileSwitcher();
  setupProfilesSettings();
  loadProfiles();
});
