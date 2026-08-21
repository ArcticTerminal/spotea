// Entry point for index.html — Home, Library, Explore, Settings, and the
// channel/playlist detail panel and player overlay drilled into from them.

import { handleInitialRoute, setupDetailPanel } from "../home/detail.js";
import { refreshRecommendations, setupExploreSearch, setupRecommendations } from "../home/explore.js";
import {
  setupHomeArtists,
  setupHorizontalScrollers,
  setupLibraryArtistGrid,
  setupLibrarySearch,
  setupMobileMenu,
  setupPreparingArtists,
  setupRefreshButton,
} from "../home/library.js";
import { resumeOverlayIfNeeded, setupPlayerOverlay } from "../home/overlay.js";
import {
  setupDownloadsOverlay,
  setupInterests,
  setupSettings,
  setupStorage,
} from "../home/settings.js";
import { setupTabs } from "../home/tabs.js";
import { setupFavorite, setupPlayer } from "../player.js";
import { installBfcacheReload, registerServiceWorker } from "../resume.js";
import { installKeyboardInset } from "../viewport.js";

installBfcacheReload();
registerServiceWorker();
installKeyboardInset();

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
setupExploreSearch();
setupRecommendations();
setupDownloadsOverlay();
setupStorage();
setupSettings();
setupInterests();
setupHomeArtists();
setupLibraryArtistGrid();
setupLibrarySearch();
setupPreparingArtists();
setupHorizontalScrollers();
// The one Refresh control covers Explore's recommendations too — passed in
// rather than imported inside library.js, which explore.js already imports
// from (see setupRefreshButton).
setupRefreshButton(refreshRecommendations);
setupMobileMenu(refreshRecommendations);
