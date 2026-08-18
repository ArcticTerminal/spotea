// Entry point for index.html — Home, Library, Explore, Settings, and the
// channel/playlist detail panel and player overlay drilled into from them.

import { setupSaveButtons } from "../content-actions.js";
import { setupBulkImport, setupBulkImportOverlay } from "../home/bulk-import.js";
import { handleInitialRoute, setupDetailPanel } from "../home/detail.js";
import { refreshRecommendations, setupExploreSearch, setupRecommendations } from "../home/explore.js";
import {
  setupHomeChannels,
  setupHorizontalScrollers,
  setupLibraryChannelGrid,
  setupLibrarySearch,
  setupMobileMenu,
  setupRefreshButton,
} from "../home/library.js";
import { setupOnboarding } from "../home/onboarding.js";
import { resumeOverlayIfNeeded, setupPlayerOverlay } from "../home/overlay.js";
import { openProfileSwitcher, setupProfiles } from "../home/profiles.js";
import {
  setupDownloadsOverlay,
  setupInterests,
  setupSettings,
  setupStorage,
} from "../home/settings.js";
import { setupTabs } from "../home/tabs.js";
import { setupFavorite, setupPlayer } from "../player.js";
import { installBfcacheReload, registerServiceWorker } from "../resume.js";

installBfcacheReload();
registerServiceWorker();

setupTabs();
setupPlayer();
setupFavorite();
setupPlayerOverlay();
setupDetailPanel();
resumeOverlayIfNeeded();
// resumeOverlayIfNeeded only reopens a track left playing in a previous
// session; a #channel/42 or #player/123 hash in the URL right now is a
// separate, higher-priority thing to resolve on boot.
handleInitialRoute();
setupSaveButtons();
setupExploreSearch();
setupRecommendations();
setupDownloadsOverlay();
setupBulkImportOverlay();
setupBulkImport();
setupStorage();
setupSettings();
setupInterests();
setupHomeChannels();
setupLibraryChannelGrid();
setupLibrarySearch();
setupHorizontalScrollers();
// The one Refresh control covers Explore's recommendations too — passed in
// rather than imported inside library.js, which explore.js already imports
// from (see setupRefreshButton).
setupRefreshButton(refreshRecommendations);
setupMobileMenu(openProfileSwitcher, refreshRecommendations);
setupProfiles();
// Last: its steps call into setupInterests's saveInterests, explore.js's
// channel search endpoint, and the bulk-import overlay's trigger id, all of
// which need to already be wired by the time it can open.
setupOnboarding();
