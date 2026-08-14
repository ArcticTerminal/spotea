// Entry point for index.html — Home, Library, Explore and Settings.

import { setupSaveButtons } from "../content-actions.js";
import { setupBulkImport, setupBulkImportOverlay } from "../home/bulk-import.js";
import { setupExploreSearch } from "../home/explore.js";
import {
  setupHomeChannels,
  setupHorizontalScrollers,
  setupLibrarySearch,
  setupMobileMenu,
  setupRefreshButton,
} from "../home/library.js";
import { resumeOverlayIfNeeded, setupPlayerOverlay } from "../home/overlay.js";
import { openProfileSwitcher, setupProfiles } from "../home/profiles.js";
import { setupDownloadsOverlay, setupSettings, setupStorage } from "../home/settings.js";
import { setupTabs } from "../home/tabs.js";
import { setupFavorite, setupPlayer } from "../player.js";
import { installBfcacheReload, registerServiceWorker } from "../resume.js";

installBfcacheReload();
registerServiceWorker();

setupTabs();
setupPlayer();
setupFavorite();
setupPlayerOverlay();
resumeOverlayIfNeeded();
setupSaveButtons();
setupExploreSearch();
setupDownloadsOverlay();
setupBulkImportOverlay();
setupBulkImport();
setupStorage();
setupSettings();
setupHomeChannels();
setupLibrarySearch();
setupHorizontalScrollers();
setupRefreshButton();
setupMobileMenu(openProfileSwitcher);
setupProfiles();
