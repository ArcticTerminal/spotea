import { api, confirmDialog, escapeHtml, setupOverlay } from "../core.js";
import { closePlayer } from "./overlay.js";

// Profile management. Wired up once on index.html — the channel/playlist
// detail panel and the player overlay share its topbar (they're all one
// document now), so switching/managing profiles works from inside either the
// same way it does from Home/Library/Explore/Settings.
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
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-user" /></svg>';

// The switch-profile overlay's handle, so the mobile menu can open it too.
let switchOverlay = null;

let cachedProfiles = [];
// Set while the manage overlay's add/rename form is mid-rename (its "Add"
// button becomes "Save" and submitting calls renameProfile instead of
// createProfile) — null means it's in its normal "add a new profile" mode.
let editingProfileId = null;
// GET /profiles used to run unconditionally on every page load to fill two
// overlays (profiles-overlay, profiles-manage-overlay) that start hidden and,
// on a typical visit, are never opened at all. Loaded lazily instead, the
// first time either one actually opens — see ensureProfilesLoaded.
let profilesLoaded = false;

// Every profile change below ends in a reload, and every one of them lands
// on Home rather than on whatever the outgoing profile happened to have open:
// all four panels are profile-scoped, and a #channel/42 hash in particular
// names a feed the incoming profile doesn't have at all. replaceState before
// reloading, because assigning a URL that differs from the current one only
// in its fragment navigates nowhere and reloads nothing.
function reloadAtHome() {
  history.replaceState(null, "", "/#home");
  window.location.reload();
}

async function fetchProfiles() {
  const { ok, data } = await api("/profiles");
  return ok ? data : [];
}

async function ensureProfilesLoaded() {
  if (profilesLoaded) return;
  profilesLoaded = true;
  await loadProfiles();
}

function updateHeaderProfileName(name) {
  const header = document.getElementById("profile-switcher-name");
  if (header) header.textContent = name;
  const mobileMenu = document.getElementById("mobile-menu-profile-name");
  if (mobileMenu) mobileMenu.textContent = name;
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
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-pencil" /></svg>
        </button>
        <button type="button" class="btn-quiet-icon profile-delete" aria-label="Delete profile">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-trash" /></svg>
        </button>
      </div>
    </li>
  `;
}

function renderProfileLists(profiles) {
  const switchList = document.getElementById("profiles-switch-list");
  if (switchList) switchList.innerHTML = profiles.map(switchRowMarkup).join("");

  const manageList = document.getElementById("profiles-manage-list");
  if (manageList) manageList.innerHTML = profiles.map(manageRowMarkup).join("");
}

async function loadProfiles() {
  cachedProfiles = await fetchProfiles();
  renderProfileLists(cachedProfiles);
}

async function switchProfile(profileId) {
  const { ok } = await api(`/profiles/${profileId}/switch`, {
    method: "POST",
    errorMessage: "Could not switch profile",
  });
  if (!ok) return;

  // Whatever's loaded belongs to the profile being left — its content id
  // means nothing under the new one. Without closing it first, the reload
  // below still fires resume.js's pagehide handler on the way out, which
  // would snapshot that now-foreign content id into sessionStorage; the
  // fresh load (already running as the new profile) then tries to resume it,
  // 404s against /content/{id}, and — since a failed resume used to never
  // clear its own stale record — repeats that same failed attempt on every
  // reload after, forever.
  closePlayer();
  reloadAtHome();
}

async function createProfile(name) {
  const { ok } = await api("/profiles", {
    method: "POST",
    body: { name },
    errorMessage: "Could not create profile",
  });
  // The new profile auto-becomes active (see routers/profiles.py) — a reload
  // is simplest, same "create and go" flow as adding a channel. Landing on
  // Home matters most here: this form lives in Settings → Manage profiles, so
  // a plain reload used to hand a brand-new profile the Settings tab with the
  // onboarding wizard sitting on top of it.
  if (ok) reloadAtHome();
}

// Returns whether the rename went through, so the caller can decide whether
// to leave the form in "editing" mode (e.g. on failure, so the typed name
// isn't lost) or reset it back to "add a new profile" mode.
async function renameProfile(profileId, name) {
  const { ok, data } = await api(`/profiles/${profileId}`, {
    method: "PUT",
    body: { name },
    errorMessage: "Could not rename profile",
  });
  if (ok) {
    await loadProfiles();
    // Every other profile change (switch, create, delete-the-current-one)
    // reloads the page, which re-renders the header from the server for
    // free. This is the one path that doesn't — renaming just refreshes the
    // overlay's own lists in place — so it's the one place the header name
    // has to be kept in sync by hand.
    if (data?.is_current) updateHeaderProfileName(data.name);
  }
  return ok;
}

async function deleteProfile(profileId) {
  const confirmed = await confirmDialog(
    "Delete this profile? Its channels and downloads will be removed too.",
    "Delete"
  );
  if (!confirmed) return;

  const { ok } = await api(`/profiles/${profileId}`, {
    method: "DELETE",
    errorMessage: "Could not delete profile",
  });
  if (!ok) return;

  // Deleting the profile you're currently viewing needs a full reload so the
  // page doesn't keep showing now-deleted content until the next natural
  // navigation — deleting some other profile just needs the lists refreshed
  // in place.
  if (cachedProfiles.find((p) => p.id === profileId)?.is_current) reloadAtHome();
  else await loadProfiles();
}

function setupSwitchOverlay() {
  // No trigger id here — opening this overlay has to wait for
  // ensureProfilesLoaded to resolve first (see openProfileSwitcher), so the
  // topbar button is wired by hand below instead of through setupOverlay's
  // own immediate-open click listener.
  switchOverlay = setupOverlay("profiles-overlay", "profiles-close", []);
  document.getElementById("profile-switcher-btn")?.addEventListener("click", () => openProfileSwitcher());

  const switchList = document.getElementById("profiles-switch-list");
  switchList?.addEventListener("click", (event) => {
    const rowBtn = event.target.closest(".profile-row-main");
    if (!rowBtn || rowBtn.disabled) return;
    switchProfile(Number(rowBtn.dataset.profileId));
  });
}

function setupManageOverlay() {
  // Same reasoning as setupSwitchOverlay above: wired by hand rather than
  // via setupOverlay's own trigger so the overlay only opens once its lists
  // are actually populated.
  const manageOverlay = setupOverlay("profiles-manage-overlay", "profiles-manage-close", []);
  if (!manageOverlay) return;
  document.getElementById("open-profiles-settings")?.addEventListener("click", async () => {
    await ensureProfilesLoaded();
    manageOverlay.open();
  });

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
    // edit isn't lost to an accidental double-press); core.js's shared
    // Escape handler closes the overlay on the second press.
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

/** Opens the switch-profile overlay — the collapsed mobile menu's profile
    row is a second entry point to the same dialog (see home/library.js). */
export async function openProfileSwitcher() {
  await ensureProfilesLoaded();
  switchOverlay?.open();
}

export function setupProfiles() {
  setupSwitchOverlay();
  setupManageOverlay();
  // Deliberately no eager fetch here — see ensureProfilesLoaded above.
}
