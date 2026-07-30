// Shared confirm dialog + toast. Loaded before the page-specific script on
// every page, so `confirmDialog()` / `showToast()` are always available and
// native window.confirm/alert are never used.

const CONFIRM_MARKUP = `
  <div class="modal" role="alertdialog" aria-modal="true" aria-labelledby="modal-message">
    <p id="modal-message"></p>
    <div class="modal-actions">
      <button type="button" id="modal-cancel" class="btn-quiet">Cancel</button>
      <button type="button" id="modal-confirm" class="btn-danger">Confirm</button>
    </div>
  </div>
`;

function ensureModal() {
  let overlay = document.getElementById("modal-overlay");
  if (overlay) return overlay;

  overlay = document.createElement("div");
  overlay.id = "modal-overlay";
  overlay.className = "modal-overlay";
  overlay.hidden = true;
  overlay.innerHTML = CONFIRM_MARKUP;
  document.body.appendChild(overlay);
  return overlay;
}

function confirmDialog(message, confirmLabel) {
  const overlay = ensureModal();
  const confirmBtn = overlay.querySelector("#modal-confirm");
  const cancelBtn = overlay.querySelector("#modal-cancel");

  overlay.querySelector("#modal-message").textContent = message;
  confirmBtn.textContent = confirmLabel || "Confirm";
  overlay.hidden = false;
  // Focus the safe choice: these dialogs guard destructive actions, so a
  // reflexive Enter should cancel rather than confirm.
  cancelBtn.focus();

  return new Promise((resolve) => {
    function cleanup(result) {
      overlay.hidden = true;
      confirmBtn.removeEventListener("click", onConfirm);
      cancelBtn.removeEventListener("click", onCancel);
      overlay.removeEventListener("click", onBackdrop);
      document.removeEventListener("keydown", onKey);
      resolve(result);
    }
    const onConfirm = () => cleanup(true);
    const onCancel = () => cleanup(false);
    const onBackdrop = (event) => {
      if (event.target === overlay) cleanup(false);
    };
    const onKey = (event) => {
      if (event.key === "Escape") cleanup(false);
    };

    confirmBtn.addEventListener("click", onConfirm);
    cancelBtn.addEventListener("click", onCancel);
    overlay.addEventListener("click", onBackdrop);
    document.addEventListener("keydown", onKey);
  });
}

function showToast(message) {
  let container = document.getElementById("toast-container");
  if (!container) {
    container = document.createElement("div");
    container.id = "toast-container";
    container.className = "toast-container";
    document.body.appendChild(container);
  }

  const toast = document.createElement("div");
  toast.className = "toast";
  toast.setAttribute("role", "status");
  toast.textContent = message;
  container.appendChild(toast);

  requestAnimationFrame(() => toast.classList.add("toast-visible"));
  setTimeout(() => {
    toast.classList.remove("toast-visible");
    setTimeout(() => toast.remove(), 250);
  }, 4000);
}
