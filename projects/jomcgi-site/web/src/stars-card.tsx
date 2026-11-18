import {
  MONTHS,
  monthBars,
  selectedWindows,
  siteName,
  type Site,
} from "./stars-model";

export function StarsCard({
  site,
  mode,
  darkness,
  nights,
  now,
  onClose,
}: {
  site: Site;
  mode: "live" | "historical";
  darkness: string;
  nights: Set<string>;
  now: number;
  onClose: () => void;
}) {
  const hours = selectedWindows(site, darkness, nights, now)
    .slice(0, 8)
    .sort((a, b) => Date.parse(a.time) - Date.parse(b.time));
  const bars = monthBars(site.clear);
  const title =
    mode === "historical"
      ? "Clear dark hours by month"
      : hours.every((h) => h.dark !== false)
        ? "Upcoming clear dark hours"
        : "Upcoming clear windows";
  return (
    <section className="stars-card" aria-label={title}>
      <header>
        <h2>{siteName(site)}</h2>
        <button type="button" onClick={onClose}>
          Close site card
        </button>
      </header>
      <h3>{title}</h3>
      {mode === "historical" ? (
        <>
          <dl className="stars-metrics">
            <div>
              <dt>Clear dark hours</dt>
              <dd>{site.clear_dark_hours ?? 0}</dd>
            </div>
            <div>
              <dt>Dark hours</dt>
              <dd>{site.dark_hours ?? 0}</dd>
            </div>
            <div>
              <dt>Clear rate</dt>
              <dd>{Math.round((site.clear_rate ?? 0) * 100)}%</dd>
            </div>
          </dl>
          <svg
            className="stars-month-chart"
            viewBox="0 0 600 190"
            role="img"
            aria-label="Clear dark hours for each month of the year"
          >
            <line
              x1="0"
              y1="155"
              x2="600"
              y2="155"
              className="stars-chart-axis"
            />
            {bars.map((bar, i) => {
              const height =
                bar.value > 0 ? Math.max(2, Math.round(bar.frac * 120)) : 0;
              return (
                <g key={bar.month}>
                  <title>
                    {bar.month}: {bar.value} clear dark hours
                  </title>
                  <rect
                    x={i * 50 + 7}
                    y={155 - height}
                    width="36"
                    height={height}
                    className={
                      bar.isMax
                        ? "stars-chart-bar stars-chart-max"
                        : "stars-chart-bar"
                    }
                  />
                  {bar.isMax && (
                    <text
                      x={i * 50 + 25}
                      y={150 - height}
                      className="stars-chart-value"
                    >
                      {bar.value}
                    </text>
                  )}
                  <text x={i * 50 + 25} y="180" className="stars-chart-label">
                    {bar.month.slice(0, 3)}
                  </text>
                </g>
              );
            })}
          </svg>
          <details>
            <summary>Monthly clear dark hours</summary>
            <table>
              <thead>
                <tr>
                  <th scope="col">Month</th>
                  <th scope="col">Clear dark hours</th>
                </tr>
              </thead>
              <tbody>
                {MONTHS.map((month, i) => (
                  <tr key={month}>
                    <th scope="row">{month}</th>
                    <td>{bars[i]!.value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
          <p>
            Hours with the sun below -12 deg and under 10% cloud, from the ERA5
            reanalysis seasonal baseline. Taller bars are the months this spot
            clears most.
          </p>
        </>
      ) : hours.length ? (
        <table>
          <thead>
            <tr>
              <th scope="col">Day</th>
              <th scope="col">Time</th>
              <th scope="col">Cloud</th>
            </tr>
          </thead>
          <tbody>
            {hours.map((hour) => (
              <tr key={hour.time}>
                <td>
                  {new Date(hour.time).toLocaleDateString("en-GB", {
                    weekday: "short",
                    day: "numeric",
                    month: "short",
                    timeZone: "Europe/London",
                  })}
                </td>
                <td>
                  <time dateTime={hour.time}>
                    {new Date(hour.time).toLocaleTimeString("en-GB", {
                      hour: "2-digit",
                      minute: "2-digit",
                      timeZone: "Europe/London",
                    })}
                  </time>
                </td>
                <td>{Math.round(hour.cloud_area_fraction)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p>No upcoming clear windows for this site.</p>
      )}
      <p>
        <a
          href={`https://www.google.com/maps/search/?api=1&query=${site.lat},${site.lon}`}
        >
          {Math.abs(site.lat).toFixed(3)}°{site.lat >= 0 ? "N" : "S"},{" "}
          {Math.abs(site.lon).toFixed(3)}°{site.lon >= 0 ? "E" : "W"}
        </a>
      </p>
    </section>
  );
}
