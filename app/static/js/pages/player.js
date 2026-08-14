// Entry point for player.html — the standalone /player/{id} page.

import { setupSaveButtons } from "../content-actions.js";
import { setupBackLink, setupFavorite, setupPlayer } from "../player.js";
import { installBfcacheReload, registerServiceWorker } from "../resume.js";

installBfcacheReload();
registerServiceWorker();

setupPlayer();
setupFavorite();
setupBackLink();
setupSaveButtons();
