// Shared by the deploy-time snapshot and the hydrated page. Only six summary
// rows enter the HTML; the full forecast stays in its content-named data file.
export const REFRESH_MS = 30 * 60_000;
// Site snapshots poll R2 every 30 minutes; the upstream stars-refresh job
// publishes forecasts every three hours (monolith/chart/values.yaml).
export const FORECAST_REFRESH_MS = 3 * 60 * 60_000;
export const EMPTY_SUMMARY = {
  asOf: null,
  snapshotAsOf: null,
  count: 0,
  rows: [],
  renderedAt: 0,
};
export const SUMMARY_START = "<!--stars-summary-->";
export const SUMMARY_END = "<!--/stars-summary-->";
const escape = (value) =>
  String(value).replace(
    /[&<>"\']/g,
    (char) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[char],
  );

export function makeSummary(snapshot, now = Date.now(), snapshotAsOf = null) {
  const sites = Array.isArray(snapshot.sites) ? snapshot.sites : [];
  const twilight = snapshot.darkness === "twilight";
  return {
    // A recent deployment or unrelated R2 object must not make old weather
    // look fresh. Only the forecast's own timestamp measures forecast age.
    asOf:
      typeof snapshot.fetched_at === "string" &&
      Number.isFinite(Date.parse(snapshot.fetched_at))
        ? new Date(snapshot.fetched_at).toISOString()
        : null,
    snapshotAsOf,
    count: snapshot.count ?? sites.length,
    renderedAt: now,
    rows: sites.slice(0, 6).map((site) => ({
      name:
        site.name ||
        `${Number(site.lat).toFixed(3)}°, ${Number(site.lon).toFixed(3)}°`,
      hours: Number(
        (twilight ? site.clear_twilight_hours : site.clear_dark_hours) ?? 0,
      ),
      twilight,
    })),
  };
}

export function isStale(asOf, now, refreshMs = FORECAST_REFRESH_MS) {
  return Boolean(asOf) && now - Date.parse(asOf) >= 3 * refreshMs;
}

export function summaryHtml(summary, now = summary.renderedAt) {
  const { asOf, snapshotAsOf, count, rows } = summary;
  const data = JSON.stringify(summary).replace(/</g, "\\u003c");
  return `${SUMMARY_START}<h2>Dark-sky forecast snapshot</h2>${
    asOf
      ? `<p>${escape(count)} dark-sky sites · As of <time datetime="${escape(asOf)}">${escape(asOf)}</time></p><p>${isStale(asOf, now) ? "Stale — showing the last available data. " : ""}Forecasts update every three hours. This page checks for updates every 30 minutes.</p>`
      : `<p>Scotland’s dark-sky sites, ranked by forecast clear, dark hours. ${rows.length ? "The forecast update time is not recorded." : "Forecast details load from this site."}</p>`
  }${
    snapshotAsOf
      ? `<p>Site data checked at <time datetime="${escape(snapshotAsOf)}">${escape(snapshotAsOf)}</time>.${isStale(snapshotAsOf, now, REFRESH_MS) ? " Stale site snapshot: showing the last available data." : ""}</p>`
      : ""
  }${
    rows.length
      ? `<ol>${rows.map((row) => `<li>${escape(row.name)} — ${escape(row.hours)} clear ${row.twilight ? "twilight" : "dark"} hours</li>`).join("")}</ol>`
      : ""
  }<script type="application/json" id="stars-summary-data">${data}</script>${SUMMARY_END}`;
}

export function stampSummary(html, summary) {
  const start = html.indexOf(SUMMARY_START);
  const end = html.indexOf(SUMMARY_END);
  if (start < 0 || end < start)
    throw new Error("stars.html is missing its summary markers");
  return (
    html.slice(0, start) +
    summaryHtml(summary) +
    html.slice(end + SUMMARY_END.length)
  );
}
