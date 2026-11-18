import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentType,
} from "react";
import { loadWalks, REFRESH_MS, type Snapshot } from "./data";
import {
  DEFAULT_FILTERS,
  filterWalks,
  LIMITS,
  LOCATIONS,
  readFilters,
  writeFilters,
  type Center,
  type Filters,
} from "./filters";
import {
  cutoff,
  dayLabel,
  isViable,
  ukDay,
  upcomingDays,
  viableDays,
} from "./viability";
import type { HikesMapProps } from "./map";
import {
  forecastStatus,
  snapshotStatus,
} from "../../../data/hikes-freshness.mjs";

const timestamp = (value: string | null) =>
  value ? <time dateTime={value}>{value}</time> : "unknown time";
const ukTime = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/London",
  hour: "2-digit",
  minute: "2-digit",
});

export function HikesPlanner() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [now, setNow] = useState(Date.now);
  const [filters, setFilters] = useState(() =>
    readFilters(new URLSearchParams(window.location.search)),
  );
  const [coords, setCoords] = useState<Center | null>(null);
  const [geoError, setGeoError] = useState("");
  const [MapView, setMapView] = useState<ComponentType<HikesMapProps> | null>(
    null,
  );
  const [mapLoading, setMapLoading] = useState(false);
  const [mapError, setMapError] = useState(false);
  const card = useRef<HTMLElement>(null);
  const geoRequest = useRef(0);

  useEffect(() => {
    const abort = new AbortController();
    async function refresh() {
      try {
        const next = await loadWalks(abort.signal);
        if (!abort.signal.aborted) {
          setSnapshot(next);
          setNow(Date.now());
          setError(false);
        }
      } catch {
        if (!abort.signal.aborted) setError(true);
      }
    }
    void refresh();
    const timer = setInterval(refresh, REFRESH_MS);
    return () => {
      abort.abort();
      clearInterval(timer);
    };
  }, [attempt]);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    const visible = () => {
      if (!document.hidden) setNow(Date.now());
    };
    document.addEventListener("visibilitychange", visible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  }, []);
  useEffect(() => {
    const url = new URL(window.location.href);
    writeFilters(url.searchParams, filters);
    window.history.replaceState(null, "", url);
  }, [filters]);
  useEffect(() => {
    const restore = () =>
      setFilters(readFilters(new URLSearchParams(window.location.search)));
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, []);
  useEffect(() => {
    const request = ++geoRequest.current;
    setCoords(null);
    if (filters.near !== "__me__") return;
    setGeoError("");
    const fail = (message: string) => {
      if (geoRequest.current !== request) return;
      setGeoError(message);
      setFilters((previous) => ({ ...previous, near: "" }));
    };
    if (!navigator.geolocation) {
      fail("Geolocation not available. Showing Anywhere.");
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (position) => {
        if (geoRequest.current === request)
          setCoords({
            lat: position.coords.latitude,
            lon: position.coords.longitude,
          });
      },
      () => {
        fail("Location permission denied or unavailable. Showing Anywhere.");
      },
      { timeout: 15_000, maximumAge: 300_000 },
    );
    return () => {
      geoRequest.current++;
    };
  }, [filters.near]);

  const walks = useMemo(
    () =>
      (snapshot?.walks ?? []).map((walk) => ({
        ...walk,
        viable_days: viableDays(walk.hours, walk.duration_h, now),
      })),
    [snapshot, now],
  );
  const covered = new Set(walks.flatMap((walk) => walk.viable_days));
  const days = upcomingDays(now).filter((day) => covered.has(day));
  const center =
    filters.near === "__me__"
      ? coords
      : (LOCATIONS.find((l) => l.key === filters.near) ?? null);
  const locationPending = filters.near === "__me__" && !coords;
  const filtered = useMemo(
    () => (locationPending ? [] : filterWalks(walks, filters, center)),
    [walks, filters, center, locationPending],
  );
  const selected = filtered.find((walk) => walk.id === filters.walk);
  const forecastAge = (fetchedAt: string | null) => (
    <span>
      Forecast fetched: {timestamp(fetchedAt)} ·{" "}
      {forecastStatus(fetchedAt, snapshot?.fetchedAt ?? null, now)}
    </span>
  );
  useEffect(() => {
    if (snapshot && !locationPending && filters.walk && !selected) {
      setFilters((previous) => ({ ...previous, walk: "" }));
    }
  }, [snapshot, locationPending, filters.walk, selected]);
  const today = ukDay(now / 1000);
  const set = (key: keyof Filters, value: string) =>
    setFilters((previous) => ({ ...previous, [key]: value }));
  const select = (id: string) => set("walk", id);
  useEffect(() => {
    if (selected) card.current?.focus();
  }, [selected?.id]);
  async function showMap() {
    setMapLoading(true);
    setMapError(false);
    try {
      const module = await import("./map");
      setMapView(() => module.HikesMap);
    } catch {
      setMapError(true);
    } finally {
      setMapLoading(false);
    }
  }
  return (
    <section aria-label="Walk planner">
      {snapshot ? (
        <div role="status">
          <p>
            Data as of {timestamp(snapshot.asOf)} ·{" "}
            {forecastStatus(snapshot.asOf, null, now)}
          </p>
          <p>
            Site snapshot: {timestamp(snapshot.generatedAt)} ·{" "}
            {snapshotStatus(snapshot.generatedAt, now)}
          </p>
          <p>Latest forecast fetch: {timestamp(snapshot.fetchedAt)}.</p>
        </div>
      ) : (
        !error && <p role="status">Loading walks…</p>
      )}
      {error && (
        <p role="status">
          Couldn’t refresh walks.{" "}
          {snapshot
            ? "Showing the last loaded data."
            : "The published walks remain below."}{" "}
          <button type="button" onClick={() => setAttempt((n) => n + 1)}>
            Retry data
          </button>
        </p>
      )}
      <p>
        {walks.length} walks ·{" "}
        {walks.filter((walk) => walk.viable_days.includes(today)).length} viable
        today · {filtered.length} shown
      </p>
      <div className="hikes-controls">
        <label>
          Near
          <select
            value={filters.near}
            onChange={(e) => {
              setGeoError("");
              set("near", e.target.value);
            }}
          >
            <option value="">Anywhere</option>
            <option value="__me__">My location</option>
            {LOCATIONS.map((location) => (
              <option key={location.key} value={location.key}>
                {location.label}
              </option>
            ))}
          </select>
        </label>
        {filters.near === "__me__" && !coords && !geoError && (
          <p role="status">Finding your location…</p>
        )}
        {geoError && <p role="alert">{geoError}</p>}
        <div
          className="hikes-days"
          role="group"
          aria-label="Filter by viable day"
        >
          {["", ...days].map((day) => (
            <button
              type="button"
              key={day}
              aria-pressed={filters.day === day}
              onClick={() => set("day", filters.day === day ? "" : day)}
            >
              {day ? dayLabel(day) : "Any day"}
            </button>
          ))}
        </div>
        <details>
          <summary>Limits</summary>
          <div className="hikes-limits">
            {LIMITS.map(([key, label, step]) => (
              <label key={key}>
                {label}
                <input
                  type="number"
                  min="0"
                  step={step}
                  value={filters[key]}
                  onChange={(e) => set(key, e.target.value)}
                />
              </label>
            ))}
          </div>
        </details>
        <button
          type="button"
          onClick={() => {
            setGeoError("");
            setFilters({ ...DEFAULT_FILTERS });
          }}
        >
          Reset
        </button>
      </div>
      {snapshot && !locationPending && !filtered.length && (
        <p role="status">
          No walks match these filters. Reset the limits to see them all.
        </p>
      )}
      {snapshot && !MapView && (
        <button type="button" onClick={showMap} disabled={mapLoading}>
          {mapLoading ? "Loading map…" : "Show map"}
        </button>
      )}
      {mapError && (
        <p role="status">
          Couldn’t load the map. Try again or choose a walk from the list.
        </p>
      )}
      {MapView && (
        <MapView
          walks={filtered}
          corpus={walks}
          selectedId={selected?.id ?? ""}
          onSelect={select}
        />
      )}
      {selected && (
        <aside
          className="hikes-card"
          aria-label={selected.name}
          tabIndex={-1}
          ref={card}
          onKeyDown={(e) => {
            if (e.key === "Escape") set("walk", "");
          }}
        >
          <header>
            <h2>
              <a href={selected.url}>{selected.name} ↗</a>
            </h2>
            <button type="button" onClick={() => set("walk", "")}>
              Close walk card
            </button>
          </header>
          <p>
            {selected.distance_km} km · {selected.ascent_m} m ascent ·{" "}
            {selected.duration_h} h
          </p>
          <p>{forecastAge(selected.forecast_fetched_at)}</p>
          {(filters.day ? [filters.day] : selected.viable_days).map((day) => (
            <div
              className="hikes-table"
              key={day}
              role="region"
              aria-label={`Hourly forecast: ${dayLabel(day)}`}
              tabIndex={0}
            >
              <table>
                <caption>Next viable: {dayLabel(day)}</caption>
                <thead>
                  <tr>
                    {["Time (UK)", "Temp", "Rain", "Wind", "Cloud"].map(
                      (name) => (
                        <th scope="col" key={name}>
                          {name}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody>
                  {selected.hours
                    .filter(
                      (hour) =>
                        hour[0] >= cutoff(now) &&
                        isViable(hour) &&
                        ukDay(hour[0]) === day,
                    )
                    .sort((a, b) => a[0] - b[0])
                    .map((hour) => (
                      <tr key={hour[0]}>
                        <th scope="row">{ukTime.format(hour[0] * 1000)}</th>
                        <td>{Math.round(hour[1])}°C</td>
                        <td>{hour[2].toFixed(1)} mm</td>
                        <td>{Math.round(hour[3])} km/h</td>
                        <td>{Math.round(hour[4])}%</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          ))}
        </aside>
      )}
      {filtered.length > 0 && (
        <details open>
          <summary>Walks with a weather window ({filtered.length})</summary>
          <ul className="hikes-results">
            {filtered.map((walk) => (
              <li key={walk.id}>
                <button type="button" onClick={() => select(walk.id)}>
                  {walk.name}
                </button>
                <span>
                  {walk.distance_km} km · {walk.ascent_m} m · {walk.duration_h}{" "}
                  h · {forecastAge(walk.forecast_fetched_at)}
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
