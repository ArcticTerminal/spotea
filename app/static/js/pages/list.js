// Entry point for content_list.html — Favorites, Saved, New Uploads and
// Recently Played. Nothing page-specific beyond the shared save buttons.

import { setupSaveButtons } from "../content-actions.js";
import { installBfcacheReload, registerServiceWorker } from "../resume.js";

installBfcacheReload();
registerServiceWorker();
setupSaveButtons();
