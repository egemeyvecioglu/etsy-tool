/**
 * Dynamic behavior for schema-driven create-job form.
 *
 * - Applies generic field visibility rules from `data-visible-when`
 * - Disables hidden fields to keep submissions clean and predictable
 * - Renders affected-listings preview from synced snapshot APIs
 * - Shows stale-sync confirmation before job submission
 */

(function () {
  var form = document.querySelector("[data-dynamic-form='true']");
  if (!form) {
    return;
  }

  var conditionalFields = Array.prototype.slice.call(
    form.querySelectorAll(".conditional-field[data-visible-when]")
  );
  var listingPreview = form.querySelector("[data-listing-preview='true']");
  var previewBody = listingPreview ? listingPreview.querySelector("[data-listing-preview-body]") : null;
  var previewMeta = listingPreview ? listingPreview.querySelector("[data-listing-preview-meta]") : null;
  var previewEndpoint = form.getAttribute("data-listing-preview-endpoint") || "";
  var previewLimit = Number(form.getAttribute("data-listing-preview-limit") || "8");
  var isConnected = form.getAttribute("data-connected") === "true";
  var submitWrap = form.querySelector("[data-submit-wrap]");
  var runJobButton = form.querySelector("[data-run-job-button]");
  var confirmPanel = form.querySelector("[data-sync-confirm-panel]");
  var confirmText = form.querySelector("[data-sync-confirm-text]");
  var confirmCancelButton = form.querySelector("[data-sync-confirm-cancel]");
  var confirmContinueButton = form.querySelector("[data-sync-confirm-continue]");
  var lastPreviewRequestId = 0;
  var previewDebounceTimer = null;
  var syncConfirmAcknowledged = false;

  function escapeName(name) {
    if (window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(name);
    }
    return String(name).replace(/(["\\])/g, "\\$1");
  }

  function controlsByName(fieldName) {
    return Array.prototype.slice.call(
      form.querySelectorAll('[name="' + escapeName(fieldName) + '"]')
    );
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function firstControlValue(fieldName) {
    var controls = controlsByName(fieldName);
    if (!controls.length) {
      return "";
    }
    return String(controls[0].value || "");
  }

  function controlValue(fieldName) {
    var controls = controlsByName(fieldName);
    if (!controls.length) {
      return "";
    }

    var first = controls[0];
    if (first.type === "radio") {
      var checkedRadio = controls.find(function (control) {
        return control.checked;
      });
      return checkedRadio ? checkedRadio.value : "";
    }

    if (first.type === "checkbox") {
      return controls
        .filter(function (control) {
          return control.checked;
        })
        .map(function (control) {
          return control.value;
        });
    }

    return first.value;
  }

  function matchesRules(rules) {
    return Object.keys(rules).every(function (controllerName) {
      var allowedValues = rules[controllerName] || [];
      var allowed = Array.isArray(allowedValues)
        ? allowedValues.map(String)
        : [String(allowedValues)];

      var current = controlValue(controllerName);
      if (Array.isArray(current)) {
        return current.some(function (value) {
          return allowed.includes(String(value));
        });
      }

      return allowed.includes(String(current));
    });
  }

  function toggleFieldControls(container, visible) {
    var controls = container.querySelectorAll("input, select, textarea, button");

    controls.forEach(function (control) {
      if (visible) {
        if (control.dataset.visibilityDisabled === "true") {
          control.disabled = false;
          delete control.dataset.visibilityDisabled;
        }

        if (control.dataset.visibilityRequired === "true") {
          control.required = true;
          delete control.dataset.visibilityRequired;
        }
        return;
      }

      if (control.required) {
        control.dataset.visibilityRequired = "true";
        control.required = false;
      }

      if (!control.disabled) {
        control.dataset.visibilityDisabled = "true";
        control.disabled = true;
      }
    });
  }

  function applyVisibilityRules() {
    conditionalFields.forEach(function (container) {
      var raw = container.getAttribute("data-visible-when") || "{}";
      var rules;

      try {
        rules = JSON.parse(raw);
      } catch (_err) {
        rules = {};
      }

      var visible = matchesRules(rules);
      container.classList.toggle("is-hidden", !visible);
      toggleFieldControls(container, visible);
    });
  }

  function listingSelectionPayload() {
    return {
      mode: String(controlValue("selection_mode") || "all_active"),
      filter: firstControlValue("filter_keyword").trim(),
      listing_ids_csv: firstControlValue("listing_ids_csv").trim(),
    };
  }

  function tAttr(attributeName) {
    if (!listingPreview) {
      return "";
    }
    return listingPreview.getAttribute(attributeName) || "";
  }

  function renderPreviewLoading() {
    if (!previewBody || !previewMeta) {
      return;
    }
    previewMeta.textContent = tAttr("data-text-loading");
    previewBody.innerHTML =
      '<tr><td colspan="3" class="muted">' + escapeHtml(tAttr("data-text-loading")) + "</td></tr>";
  }

  function formatTemplate(template, values) {
    return template.replace(/\{(\w+)\}/g, function (_full, key) {
      if (Object.prototype.hasOwnProperty.call(values, key)) {
        return String(values[key]);
      }
      return "";
    });
  }

  function renderPreviewRows(payload, mode) {
    if (!previewBody || !previewMeta) {
      return;
    }

    var rows = Array.isArray(payload.results) ? payload.results : [];
    var totalCount = Number(payload.count || 0);
    var hasSynced = Boolean(payload.has_synced);
    var unknownTitle = tAttr("data-text-unknown-title");

    if (!hasSynced && mode !== "listing_ids") {
      previewMeta.textContent = tAttr("data-text-no-sync");
      previewBody.innerHTML =
        '<tr><td colspan="3" class="muted">' + escapeHtml(tAttr("data-text-no-sync")) + "</td></tr>";
      return;
    }

    if (!rows.length) {
      previewMeta.textContent = tAttr("data-text-empty");
      previewBody.innerHTML =
        '<tr><td colspan="3" class="muted">' + escapeHtml(tAttr("data-text-empty")) + "</td></tr>";
      return;
    }

    var summaryTemplate = tAttr("data-text-summary-template");
    previewMeta.textContent = formatTemplate(summaryTemplate, {
      shown: rows.length,
      count: totalCount,
    });

    if (totalCount > rows.length) {
      var truncatedTemplate = tAttr("data-text-truncated-template");
      previewMeta.textContent +=
        " " +
        formatTemplate(truncatedTemplate, {
          remaining: totalCount - rows.length,
        });
    }

    var inSyncText = tAttr("data-text-in-sync");
    var notInSyncText = tAttr("data-text-not-in-sync");

    previewBody.innerHTML = rows
      .map(function (row) {
        var listingId = escapeHtml(row.listing_id || "");
        var title = escapeHtml(row.title || unknownTitle);
        var inSync = row.in_sync_snapshot !== false;
        var pillClass = inSync ? "succeeded" : "failed";
        var pillLabel = inSync ? inSyncText : notInSyncText;

        return (
          "<tr>" +
          "<td>" +
          listingId +
          "</td>" +
          "<td>" +
          title +
          "</td>" +
          '<td><span class="pill ' +
          pillClass +
          '">' +
          escapeHtml(pillLabel) +
          "</span></td>" +
          "</tr>"
        );
      })
      .join("");
  }

  function refreshListingPreview() {
    if (!isConnected || !listingPreview || !previewEndpoint) {
      return;
    }

    var selection = listingSelectionPayload();
    var requestId = ++lastPreviewRequestId;
    var params = new URLSearchParams();
    params.set("mode", selection.mode);
    params.set("limit", String(Number.isFinite(previewLimit) ? previewLimit : 8));

    if (selection.filter) {
      params.set("filter", selection.filter);
    }
    if (selection.listing_ids_csv) {
      params.set("listing_ids_csv", selection.listing_ids_csv);
    }

    renderPreviewLoading();

    fetch(previewEndpoint + "?" + params.toString(), {
      headers: {
        Accept: "application/json",
      },
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("Preview request failed");
        }
        return response.json();
      })
      .then(function (payload) {
        if (requestId !== lastPreviewRequestId) {
          return;
        }
        renderPreviewRows(payload, selection.mode);
      })
      .catch(function () {
        if (requestId !== lastPreviewRequestId) {
          return;
        }
        if (previewMeta) {
          previewMeta.textContent = tAttr("data-text-empty");
        }
        if (previewBody) {
          previewBody.innerHTML =
            '<tr><td colspan="3" class="muted">' + escapeHtml(tAttr("data-text-empty")) + "</td></tr>";
        }
      });
  }

  function debouncePreviewRefresh() {
    if (previewDebounceTimer) {
      window.clearTimeout(previewDebounceTimer);
    }
    previewDebounceTimer = window.setTimeout(refreshListingPreview, 220);
  }

  function hideSyncConfirmPanel() {
    if (!confirmPanel) {
      return;
    }
    confirmPanel.classList.add("is-hidden");
    if (submitWrap) {
      submitWrap.classList.remove("confirm-open");
    }
    if (runJobButton) {
      runJobButton.classList.remove("is-hidden");
    }
  }

  function isSyncConfirmOpen() {
    return Boolean(confirmPanel && !confirmPanel.classList.contains("is-hidden"));
  }

  function showSyncConfirmPanel(message) {
    if (!confirmPanel || !confirmText) {
      return;
    }
    confirmText.textContent = message;
    confirmPanel.classList.remove("is-hidden");
    if (submitWrap) {
      submitWrap.classList.add("confirm-open");
    }
    if (runJobButton) {
      runJobButton.classList.add("is-hidden");
    }
    if (confirmContinueButton) {
      confirmContinueButton.focus();
    }
  }

  function submitAfterConfirmation() {
    syncConfirmAcknowledged = true;
    hideSyncConfirmPanel();

    if (typeof form.requestSubmit === "function") {
      if (runJobButton) {
        form.requestSubmit(runJobButton);
      } else {
        form.requestSubmit();
      }
      return;
    }

    form.submit();
  }

  function maybeConfirmSyncAge(event) {
    if (!isConnected) {
      return;
    }

    var message = (form.getAttribute("data-sync-confirm-message") || "").trim();
    if (!message) {
      return;
    }

    if (syncConfirmAcknowledged) {
      syncConfirmAcknowledged = false;
      return;
    }

    event.preventDefault();
    showSyncConfirmPanel(message);
  }

  form.addEventListener("change", function () {
    applyVisibilityRules();
    debouncePreviewRefresh();
    syncConfirmAcknowledged = false;
  });
  form.addEventListener("input", function () {
    applyVisibilityRules();
    debouncePreviewRefresh();
    syncConfirmAcknowledged = false;
  });
  form.addEventListener("submit", maybeConfirmSyncAge);

  if (confirmCancelButton) {
    confirmCancelButton.addEventListener("click", function () {
      syncConfirmAcknowledged = false;
      hideSyncConfirmPanel();
    });
  }

  if (confirmContinueButton) {
    confirmContinueButton.addEventListener("click", submitAfterConfirmation);
  }

  document.addEventListener(
    "pointerdown",
    function (event) {
      if (!isSyncConfirmOpen()) {
        return;
      }

      var target = event.target;
      if (!target) {
        return;
      }

      if (confirmPanel && confirmPanel.contains(target)) {
        return;
      }

      hideSyncConfirmPanel();
      syncConfirmAcknowledged = false;
    },
    true
  );

  applyVisibilityRules();
  refreshListingPreview();
  hideSyncConfirmPanel();
})();
