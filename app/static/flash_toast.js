/**
 * Auto-dismisses small toast notifications after a short delay.
 */

(function () {
  var toasts = document.querySelectorAll("[data-toast-auto-dismiss-ms]");
  if (!toasts.length) {
    return;
  }

  // If signed-out toast came from query string, consume it once so refresh
  // does not replay the same toast.
  try {
    var currentUrl = new URL(window.location.href);
    if (currentUrl.searchParams.get("message_key") === "flash.signed_out") {
      currentUrl.searchParams.delete("message_key");
      var nextUrl =
        currentUrl.pathname +
        (currentUrl.search ? currentUrl.search : "") +
        (currentUrl.hash ? currentUrl.hash : "");
      window.history.replaceState({}, "", nextUrl);
    }
  } catch (_err) {
    // Ignore URL API issues and continue normal toast behavior.
  }

  toasts.forEach(function (toast) {
    var raw = Number(toast.getAttribute("data-toast-auto-dismiss-ms") || "3200");
    var timeoutMs = Number.isFinite(raw) && raw > 0 ? raw : 3200;

    window.setTimeout(function () {
      toast.classList.add("is-hiding");
      window.setTimeout(function () {
        if (toast && toast.parentNode) {
          toast.parentNode.removeChild(toast);
        }
      }, 220);
    }, timeoutMs);
  });
})();
