// The snapshot runs after the Vite build. Fill the pre-rendered page's inert
// summary with the SAME data we are about to deploy; no R2 reads at build time.
import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import {
  forecastStatus,
  snapshotStatus,
  oldestForecast,
  timestamp,
} from "./hikes-freshness.mjs";

const escape = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (char) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[char],
  );

export async function publishHikesSummary(out, now = new Date()) {
  const index = JSON.parse(
    await readFile(join(out, "hikes/latest.json"), "utf8"),
  );
  const path = index.files.walks;
  if (!/^\/data\/hikes\/v\/walks-[a-f0-9]{12}\.json$/.test(path))
    throw new Error("Invalid hikes snapshot path");
  const data = JSON.parse(
    await readFile(join(out, path.slice("/data/".length)), "utf8"),
  );
  if (data.schema_version !== 2 || !Array.isArray(data.walks))
    throw new Error("Unsupported hikes snapshot");
  const walks = data.walks
    .filter(
      (w) =>
        w.duration_h > 0 &&
        w.name &&
        /^https:\/\/(www\.)?walkhighlands\.co\.uk\//.test(w.url),
    )
    .sort((a, b) => a.name.localeCompare(b.name));
  const latest = timestamp(data.fetched_at);
  const oldest = oldestForecast(walks.map((w) => w.forecast_fetched_at));
  const unknown = walks.filter((w) => !timestamp(w.forecast_fetched_at)).length;
  const time = (at) =>
    at ? `<time datetime="${escape(at)}">${escape(at)}</time>` : "unknown time";
  const status = (at) =>
    `<span data-forecast-freshness data-fetched-at="${escape(at ?? "")}" data-latest-fetched-at="${escape(latest ?? "")}">${forecastStatus(at, latest, now.getTime())}</span>`;
  const summary =
    `<p>Data as of ${time(timestamp(index.asOf))} · ${status(timestamp(index.asOf))}</p>` +
    `<p>Site snapshot: ${time(timestamp(index.generatedAt))} · <span data-snapshot-freshness data-generated-at="${escape(timestamp(index.generatedAt) ?? "")}">${snapshotStatus(index.generatedAt, now.getTime())}</span></p>` +
    `<p>Latest forecast fetch: ${time(latest)}. Oldest walk forecast: ${time(oldest)}. ${status(oldest)}</p>` +
    (unknown
      ? `<p>${unknown} walks have unknown forecast age; weather windows use retained data.</p>`
      : "") +
    `<p>${walks.length} Scottish walks in the published forecast. Explore walks to find their weather windows.</p>` +
    `<ul>${walks
      .slice(0, 8)
      .map(
        (w) =>
          `<li><a href="${escape(w.url)}">${escape(w.name)}</a> · ${escape(w.distance_km)} km · ${escape(w.ascent_m)} m ascent · ${escape(w.duration_h)} h. Forecast fetched: ${time(timestamp(w.forecast_fetched_at))} · ${status(timestamp(w.forecast_fetched_at))}</li>`,
      )
      .join("")}</ul>`;
  const file = join(out, "../hikes.html");
  const html = await readFile(file, "utf8");
  const slot = /<!--hikes-summary:start-->[\s\S]*?<!--hikes-summary:end-->/g;
  if ([...html.matchAll(slot)].length !== 1)
    throw new Error("hikes.html must have one published summary slot");
  await writeFile(
    file,
    html.replace(
      slot,
      () => `<!--hikes-summary:start-->${summary}<!--hikes-summary:end-->`,
    ),
  );
}
