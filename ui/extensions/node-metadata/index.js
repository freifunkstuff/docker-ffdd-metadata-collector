const STATUS_URL = "/node-metadata-status.json";

function formatTimestamp(value) {
  if (!value) {
    return "-";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return String(value);
  }
  return parsed.toLocaleString("de-DE");
}

function numberOrDash(value) {
  return value == null ? "-" : String(value);
}

function getStatus(context) {
  return context?.data?.[STATUS_URL] || null;
}

function renderError(context) {
  if (!context?.error) {
    return "";
  }

  return `<div class="error-box">Fehler beim Laden: ${escapeHtml(context.error)}</div>`;
}

function renderCards(status) {
  const nodes = status?.nodes || {};
  const fetch = status?.fetch || {};
  const cards = [
    ["Gesamt", numberOrDash(nodes.total)],
    ["Online", numberOrDash(nodes.online)],
    ["Stale", numberOrDash(nodes.stale)],
    ["Fetches", numberOrDash(fetch.fetches)],
    ["Fetches/min", numberOrDash(fetch.ratePerMinute)],
  ];

  return `
    <section class="cards">
      ${cards
        .map(
          ([label, value]) => `
            <article class="card">
              <h3>${escapeHtml(label)}</h3>
              <strong>${escapeHtml(value)}</strong>
            </article>
          `,
        )
        .join("")}
    </section>
  `;
}

function renderTableRows(status) {
  const collector = status?.collector || {};
  const fetch = status?.fetch || {};
  const nodes = status?.nodes || {};
  const rows = [
    ["Status berechnet", formatTimestamp(status?.generatedAt)],
    ["Letzter Fetch", formatTimestamp(fetch.lastFetchAt)],
    ["Letzter erfolgreicher Fetch", formatTimestamp(fetch.lastSuccessfulFetchAt)],
    ["Quelle", numberOrDash(collector.source)],
    ["Source-Type", numberOrDash(collector.sourceType)],
    ["Online-Fenster", numberOrDash(collector.onlineWindowSeconds)],
    ["Fetch-Fenster", numberOrDash(fetch.windowSeconds)],
    ["Mit Info", numberOrDash(nodes.withInfo)],
    ["Mit Fetch-Fehler", numberOrDash(nodes.withFetchError)],
  ];

  return rows
    .map(
      ([label, value]) => `
        <tr>
          <th>${escapeHtml(label)}</th>
          <td>${escapeHtml(value)}</td>
        </tr>
      `,
    )
    .join("");
}

function renderLoading(container) {
  container.innerHTML = `
    <p class="muted">Lade Statusdaten ...</p>
  `;
}

export function render(container, context) {
  const status = getStatus(context);

  if (!status && !context?.error) {
    renderLoading(container);
  } else {
    container.innerHTML = `
      ${renderError(context)}
      ${renderCards(status)}
      <table class="kv-table"><tbody>${renderTableRows(status)}</tbody></table>
    `;
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function dispose(container) {
  container.textContent = "";
}