/**
 * Opens Etsy OAuth in a popup window and handles callback messages.
 */

(function () {
  function openCenteredPopup(url) {
    var width = 560;
    var height = 760;
    var left = window.screenX + Math.max(0, (window.outerWidth - width) / 2);
    var top = window.screenY + Math.max(0, (window.outerHeight - height) / 2);

    var features = [
      "popup=yes",
      "toolbar=no",
      "menubar=no",
      "location=yes",
      "status=no",
      "resizable=yes",
      "scrollbars=yes",
      "width=" + width,
      "height=" + height,
      "left=" + Math.round(left),
      "top=" + Math.round(top),
    ].join(",");

    return window.open(url, "etsy_oauth_popup", features);
  }

  function addPopupQuery(baseHref) {
    var url = new URL(baseHref, window.location.origin);
    url.searchParams.set("popup", "1");
    return url.toString();
  }

  function attachPopupHandlers() {
    var links = document.querySelectorAll("[data-oauth-popup='true']");
    links.forEach(function (link) {
      link.addEventListener("click", function (event) {
        event.preventDefault();
        var popupUrl = addPopupQuery(link.getAttribute("href") || "/auth/etsy/start");
        var popup = openCenteredPopup(popupUrl);

        if (!popup) {
          // Popup blocked: fall back to full-page navigation.
          window.location.href = popupUrl;
          return;
        }

        popup.focus();
      });
    });
  }

  function handleOAuthMessage(event) {
    if (event.origin !== window.location.origin) {
      return;
    }

    var payload = event.data || {};
    if (payload.type !== "etsy_oauth_result") {
      return;
    }

    if (payload.success) {
      window.location.href = "/dashboard?message_key=flash.etsy_connected";
      return;
    }

    var target = new URL(window.location.href);
    target.searchParams.set("error", payload.error || "OAuth failed");
    window.location.href = target.toString();
  }

  attachPopupHandlers();
  window.addEventListener("message", handleOAuthMessage);
})();
