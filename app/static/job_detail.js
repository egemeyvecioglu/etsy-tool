/**
 * Polling script for live job progress updates and cancellation handling.
 */

(function () {
  const root = document.getElementById("job-root");
  if (!root) {
    return;
  }

  const jobId = root.getAttribute("data-job-id");
  const statusEl = document.getElementById("job-status");
  const progressTextEl = document.getElementById("progress-text");
  const countsTextEl = document.getElementById("counts-text");
  const fillEl = document.getElementById("progress-fill");
  const bodyEl = document.getElementById("job-items-body");
  const cancelButton = document.getElementById("cancel-job-btn");

  const statusLabels = JSON.parse(root.getAttribute("data-status-labels") || "{}");
  const progressTemplate = root.getAttribute("data-progress-template") || "{completed}/{total}";
  const countsTemplate = root.getAttribute("data-counts-template") || "{succeeded}/{failed}/{skipped}";
  const notAvailable = root.getAttribute("data-not-available") || "N/A";

  const terminalStatuses = new Set(["SUCCEEDED", "FAILED", "CANCELLED"]);

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = String(value ?? "");
    return div.innerHTML;
  }

  function normalizeStatus(status) {
    return String(status || "").toLowerCase();
  }

  function localizeStatus(status) {
    return statusLabels[String(status)] || String(status || "");
  }

  function formatMessage(template, values) {
    let output = String(template || "");
    for (const [key, value] of Object.entries(values)) {
      output = output.replaceAll(`{${key}}`, String(value));
    }
    return output;
  }

  function renderSummary(value) {
    if (value === null || value === undefined) {
      return notAvailable;
    }
    return JSON.stringify(value);
  }

  function renderRows(rows) {
    bodyEl.innerHTML = "";

    for (const row of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(row.listing_id)}</td>
        <td><span class="pill ${normalizeStatus(row.status)}">${escapeHtml(localizeStatus(row.status))}</span></td>
        <td>${escapeHtml(renderSummary(row.before_summary))}</td>
        <td>${escapeHtml(renderSummary(row.after_summary))}</td>
        <td>${escapeHtml(row.error || "")}</td>
      `;
      bodyEl.appendChild(tr);
    }
  }

  async function fetchJson(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
      throw new Error(`Request failed: ${response.status}`);
    }
    return response.json();
  }

  async function refresh() {
    const summary = await fetchJson(`/api/jobs/${jobId}`);
    const rowsPayload = await fetchJson(`/api/jobs/${jobId}/items`);

    statusEl.textContent = localizeStatus(summary.status);
    progressTextEl.textContent = formatMessage(progressTemplate, {
      completed: summary.completed_items,
      total: summary.total_items,
    });
    countsTextEl.textContent = formatMessage(countsTemplate, {
      succeeded: summary.succeeded_items,
      failed: summary.failed_items,
      skipped: summary.skipped_items,
    });

    const percent = summary.total_items > 0 ? (summary.completed_items / summary.total_items) * 100 : 0;
    fillEl.style.width = `${percent.toFixed(2)}%`;

    renderRows(rowsPayload.results || []);

    if (terminalStatuses.has(summary.status)) {
      cancelButton.disabled = true;
      return false;
    }

    if (summary.cancel_requested) {
      cancelButton.disabled = true;
    }

    return true;
  }

  let timer = null;

  async function tick() {
    try {
      const keepPolling = await refresh();
      if (!keepPolling && timer) {
        clearInterval(timer);
      }
    } catch (_err) {
      // If polling fails briefly, keep retrying on next interval.
    }
  }

  cancelButton?.addEventListener("click", async function () {
    cancelButton.disabled = true;
    try {
      await fetchJson(`/api/jobs/${jobId}/cancel`, { method: "POST" });
      await tick();
    } catch (_err) {
      cancelButton.disabled = false;
    }
  });

  timer = setInterval(tick, 2500);
  tick();
})();
