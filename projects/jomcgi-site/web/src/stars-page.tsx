import {
  Component,
  lazy,
  Suspense,
  type ReactNode,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Shell } from "./Shell";
import {
  EMPTY_SUMMARY,
  REFRESH_MS,
  isStale,
  makeSummary,
  summaryHtml,
  type Summary,
} from "../../data/stars-summary.mjs";
import {
  DEFAULT_VIEW,
  MONTHS,
  dataPath,
  getJson,
  historyView,
  liveHeat,
  nightLabel,
  readStarsParams,
  shareForecastHours,
  siteName,
  starsNights,
  writeStarsParams,
  type DataIndex,
  type Site,
  type Snapshot,
} from "./stars-model";
import { StarsCard } from "./stars-card";
import "./stars.css";

const StarsMap = lazy(() => import("./stars-map"));
class MapBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <p role="status">
        The map could not load. The site list and details remain available.
        Reload the page to retry.
      </p>
    ) : (
      this.props.children
    );
  }
}
function initialSummary(): Summary {
  if (typeof document === "undefined") return EMPTY_SUMMARY;
  const data = document.getElementById("stars-summary-data")?.textContent;
  return data ? JSON.parse(data) : EMPTY_SUMMARY;
}

export function StarsPage() {
  const [summary, setSummary] = useState(initialSummary);
  const [now, setNow] = useState(() => summary.renderedAt);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [index, setIndex] = useState<DataIndex | null>(null);
  const [view, setView] = useState(DEFAULT_VIEW);
  const [dataWanted, setDataWanted] = useState(false);
  const [mapWanted, setMapWanted] = useState(false);
  const [selectedId, setSelectedId] = useState<Site["id"] | null>(null);
  const [history, setHistory] = useState<{
    path: string;
    sites: Site[];
    asOf: string | null;
  } | null>(null);
  const [liveError, setLiveError] = useState(false);
  const [summaryError, setSummaryError] = useState(false);
  const [historyError, setHistoryError] = useState(false);
  const [retry, setRetry] = useState(0);
  const historyRequests = useRef(new Map<string, Promise<Snapshot>>());
  const forecastPath = useRef<string | null>(null);
  const [sitePage, setSitePage] = useState(0);

  useEffect(() => {
    const readView = () => {
      const value = readStarsParams(new URLSearchParams(location.search));
      setView(value);
      if (value.mode === "historical" || value.selectedNight !== "all")
        setDataWanted(true);
    };
    readView();
    window.addEventListener("popstate", readView);
    setNow(Date.now());
    const clockTimer = setInterval(() => setNow(Date.now()), 60_000);
    return () => {
      clearInterval(clockTimer);
      window.removeEventListener("popstate", readView);
    };
  }, []);

  useEffect(() => {
    let alive = true;
    let busy = false;
    const refresh = async () => {
      if (busy) return;
      busy = true;
      try {
        const latest = await getJson<DataIndex>("/data/stars/latest.json");
        if (!alive) return;
        setIndex(latest);
        try {
          const updated = await getJson<Summary>(
            dataPath(latest.files.summary),
          );
          if (alive) {
            setSummary(updated);
            setSummaryError(false);
          }
        } catch {
          if (alive) setSummaryError(true);
        }
        if (!alive || !dataWanted) return;
        try {
          const path = dataPath(latest.files.sites);
          if (forecastPath.current !== path) {
            const data = shareForecastHours(await getJson<Snapshot>(path));
            if (!alive) return;
            setSnapshot(data);
            // The browser fetch time cannot prove that the site's snapshot
            // job ran. Keep the publication timestamp from latest.json.
            setSummary(makeSummary(data, Date.now(), latest.generatedAt));
            setSummaryError(false);
            forecastPath.current = path;
          }
          if (alive) setLiveError(false);
        } catch {
          if (alive) setLiveError(true);
        }
      } catch {
        if (alive) {
          setSummaryError(true);
          if (dataWanted) setLiveError(true);
        }
      } finally {
        busy = false;
      }
    };
    // Every visit refreshes the small summary after first paint and on a timer.
    // Full forecast and historical payloads still require user or URL intent.
    const paint = requestAnimationFrame(() => {
      if (alive) {
        setNow(Date.now());
        void refresh();
      }
    });
    const refreshTimer = setInterval(refresh, REFRESH_MS);
    return () => {
      alive = false;
      cancelAnimationFrame(paint);
      clearInterval(refreshTimer);
    };
  }, [dataWanted, retry]);

  useEffect(() => {
    if (view.mode !== "historical" || !index) return;
    let alive = true;
    let path: string;
    try {
      path = dataPath(index.files.historyMap);
    } catch {
      setHistoryError(true);
      return;
    }
    // History's own R2 time: a newer forecast sets the index-wide asOf.
    // stars-refresh rewrites it every 3 hours (9 hours is stale); unchanged
    // rows keep their content-named path but get a newer time.
    const asOf = index.sources?.historyMap?.lastModified ?? null;
    if (history?.path === path) {
      setHistory((h) => (h && h.asOf !== asOf ? { ...h, asOf } : h));
      return;
    }
    setHistoryError(false);
    let request = historyRequests.current.get(path);
    if (!request) {
      historyRequests.current.clear();
      request = getJson<Snapshot>(path);
      historyRequests.current.set(path, request);
    }
    request
      .then((data) => {
        if (alive) setHistory({ path, sites: data.sites, asOf });
      })
      .catch(() => {
        historyRequests.current.delete(path);
        if (alive) setHistoryError(true);
      });
    return () => {
      alive = false;
    };
  }, [view.mode, index, history?.path, retry]);

  const darkness = snapshot?.darkness ?? "astronomical";
  const sites = snapshot?.sites;
  const nights = useMemo(
    () => starsNights(sites ?? [], darkness, now),
    [sites, darkness, now],
  );
  const effectiveNight = nights.includes(view.selectedNight)
    ? view.selectedNight
    : "all";
  useEffect(() => {
    if (
      !snapshot ||
      view.mode !== "live" ||
      view.selectedNight === effectiveNight
    )
      return;
    const next = { ...view, selectedNight: effectiveNight };
    setView(next);
    const url = new URL(location.href);
    writeStarsParams(url.searchParams, next);
    window.history.replaceState(null, "", url);
  }, [snapshot, view, effectiveNight]);
  const activeNights = useMemo(
    () => new Set(effectiveNight === "all" ? nights : [effectiveNight]),
    [effectiveNight, nights],
  );
  const rows = useMemo(
    () =>
      view.mode === "historical"
        ? historyView(history?.sites ?? [], view.selectedMonth)
        : (sites ?? [])
            .map((site) => ({
              site,
              score: liveHeat(site, darkness, activeNights, now),
            }))
            .filter(({ score }) => score > 0)
            .sort((a, b) => b.score - a.score)
            .map(({ site }) => site),
    [
      view.mode,
      view.selectedMonth,
      history,
      sites,
      darkness,
      activeNights,
      now,
    ],
  );
  const selected = rows.find((site) => site.id === selectedId);
  const lastPage = Math.max(0, Math.ceil(rows.length / 30) - 1);
  const currentPage = Math.min(sitePage, lastPage);
  const pageStart = currentPage * 30;
  useEffect(
    () => setSitePage(0),
    [view.mode, view.selectedMonth, effectiveNight],
  );
  function change(next: Partial<typeof view>) {
    setDataWanted(true);
    const value = { ...view, ...next };
    setView(value);
    if (value.mode !== view.mode) setSelectedId(null);
    const url = new URL(location.href);
    writeStarsParams(url.searchParams, value);
    window.history.replaceState(null, "", url);
  }
  return (
    <Shell>
      <header className="stars-heading">
        <h1>Dark-sky stargazing map, Scotland viewing windows</h1>
        <p>
          Scotland&apos;s dark-sky sites ranked by forecast clear, dark hours.
        </p>
      </header>
      <section
        id="stars-summary"
        aria-label="Forecast snapshot"
        dangerouslySetInnerHTML={{ __html: summaryHtml(summary, now) }}
      />
      {summaryError && !liveError && (
        <p role="status">
          Could not refresh the summary. Showing the last available data.{" "}
          <button type="button" onClick={() => setRetry((n) => n + 1)}>
            Retry summary
          </button>
        </p>
      )}
      {!dataWanted && (
        <p>
          <button
            type="button"
            className="stars-load-forecast"
            onClick={() => setDataWanted(true)}
          >
            Load live forecasts
          </button>
        </p>
      )}
      {dataWanted && !snapshot && !liveError && (
        <p role="status">
          Loading forecast… The snapshot remains available above.
        </p>
      )}
      {liveError && (
        <p role="status">
          {summary.asOf
            ? "Could not refresh. Showing the last available data."
            : "Could not load the forecast."}{" "}
          <button type="button" onClick={() => setRetry((n) => n + 1)}>
            Retry forecast
          </button>
        </p>
      )}
      <div className="stars-controls">
        <div role="group" aria-label="Map mode" className="stars-chips">
          <button
            type="button"
            aria-pressed={view.mode === "live"}
            onClick={() => change({ mode: "live" })}
          >
            Live
          </button>
          <button
            type="button"
            aria-pressed={view.mode === "historical"}
            onClick={() => change({ mode: "historical" })}
          >
            Historical
          </button>
        </div>
        {view.mode === "live" ? (
          <div role="group" aria-label="Night" className="stars-chips">
            <button
              type="button"
              aria-pressed={effectiveNight === "all"}
              onClick={() => change({ selectedNight: "all" })}
            >
              All nights
            </button>
            {nights.map((night) => (
              <button
                type="button"
                key={night}
                aria-pressed={effectiveNight === night}
                onClick={() => change({ selectedNight: night })}
              >
                {nightLabel(night)}
              </button>
            ))}
          </div>
        ) : (
          <div role="group" aria-label="Month" className="stars-chips">
            <button
              type="button"
              aria-pressed={!view.selectedMonth}
              onClick={() => change({ selectedMonth: 0 })}
            >
              All year
            </button>
            {MONTHS.map((month, i) => (
              <button
                type="button"
                key={month}
                aria-pressed={view.selectedMonth === i + 1}
                onClick={() => change({ selectedMonth: i + 1 })}
              >
                {month}
              </button>
            ))}
          </div>
        )}
      </div>
      {view.mode === "live" && darkness === "twilight" && (
        <p role="status">
          No astronomical darkness in Scotland right now (it returns by August).
          Showing the darkest twilight windows instead, best in the south.
        </p>
      )}
      {view.mode === "live" && darkness === "none" && (
        <p role="status">
          The Scottish sky never gets properly dark at this time of year, so
          there is nothing worth planning this week. Real darkness returns by
          August.
        </p>
      )}
      {view.mode === "historical" && !history && !historyError && (
        <p role="status">
          Loading history… The forecast snapshot remains above.
        </p>
      )}
      {view.mode === "historical" && history?.asOf && (
        <p>
          History as of <time dateTime={history.asOf}>{history.asOf}</time>.
          {isStale(history.asOf, now) &&
            " Stale — showing the last available data."}
        </p>
      )}
      {view.mode === "historical" && historyError && (
        <p role="status">
          {history
            ? "Could not refresh history. Showing the last available data."
            : "Could not load historical data. The forecast snapshot remains above."}{" "}
          <button type="button" onClick={() => setRetry((n) => n + 1)}>
            Retry history
          </button>
        </p>
      )}
      <section aria-label="Dark-sky map" className="stars-map-section">
        {mapWanted ? (
          <MapBoundary>
            <Suspense
              fallback={
                <p role="status">
                  Loading map… The site list remains available below.
                </p>
              }
            >
              <StarsMap
                sites={rows}
                mode={view.mode}
                darkness={darkness}
                activeNights={activeNights}
                now={now}
                onSelect={setSelectedId}
              />
            </Suspense>
          </MapBoundary>
        ) : (
          <button
            type="button"
            className="stars-load-map"
            onClick={() => {
              setDataWanted(true);
              setMapWanted(true);
            }}
          >
            Load interactive map
          </button>
        )}
        <div
          className="stars-legend"
          aria-label={
            darkness === "twilight" && view.mode === "live"
              ? "Clear twilight hours"
              : "Clear dark hours"
          }
        >
          <span>
            Clear{" "}
            {darkness === "twilight" && view.mode === "live"
              ? "twilight"
              : "dark"}{" "}
            hours:
          </span>
          <span>
            <i className="stars-low" /> Low
          </span>
          <span>
            <i className="stars-mid" /> Medium
          </span>
          <span>
            <i className="stars-high" /> High
          </span>
        </div>
      </section>
      {selected && (
        <StarsCard
          site={selected}
          mode={view.mode}
          darkness={darkness}
          nights={activeNights}
          now={now}
          onClose={() => setSelectedId(null)}
        />
      )}
      {(view.mode === "live" ? snapshot : history) && (
        <section aria-labelledby="stars-sites-heading">
          <h2 id="stars-sites-heading">
            {view.mode === "historical"
              ? `Sites with history ${view.selectedMonth ? `in ${MONTHS[view.selectedMonth - 1]}` : "across the year"}`
              : "Sites with upcoming viewing windows"}
          </h2>
          <p>
            {rows.length} sites. Select a site to see{" "}
            {view.mode === "live" ? "its clear hours" : "its 12-month chart"}.
          </p>
          {rows.length ? (
            <>
              <p role="status">
                Showing {pageStart + 1}–{Math.min(pageStart + 30, rows.length)}{" "}
                of {rows.length} sites.
              </p>
              <ul className="stars-site-list">
                {rows.slice(pageStart, pageStart + 30).map((site) => (
                  <li key={site.id}>
                    <button
                      type="button"
                      aria-pressed={selectedId === site.id}
                      onClick={() => setSelectedId(site.id)}
                    >
                      {siteName(site)}
                    </button>{" "}
                    —{" "}
                    {view.mode === "live"
                      ? liveHeat(site, darkness, activeNights, now)
                      : site.clear_dark_hours}{" "}
                    clear{" "}
                    {darkness === "twilight" && view.mode === "live"
                      ? "twilight"
                      : "dark"}{" "}
                    hours
                  </li>
                ))}
              </ul>
              <nav aria-label="Site pages" className="stars-chips">
                <button
                  type="button"
                  disabled={currentPage === 0}
                  onClick={() => setSitePage(currentPage - 1)}
                >
                  Previous sites
                </button>
                <span>
                  Page {currentPage + 1} of {lastPage + 1}
                </span>
                <button
                  type="button"
                  disabled={currentPage === lastPage}
                  onClick={() => setSitePage(currentPage + 1)}
                >
                  Next sites
                </button>
              </nav>
            </>
          ) : (
            <p>
              {view.mode === "live"
                ? "No dark-sky windows in the next few nights. Check back after the next forecast refresh."
                : "No clear dark hours for this selection. Try another month."}
            </p>
          )}
        </section>
      )}
      <p className="stars-credits">
        Map: <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>{" "}
        / <a href="https://openfreemap.org/">OpenFreeMap</a>. Weather:{" "}
        <a href="https://www.met.no/en">MET Norway</a> (
        <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>).
        Climatology: <a href="https://climate.copernicus.eu/">Copernicus</a>,
        ERA5 reanalysis seasonal baseline.
      </p>
    </Shell>
  );
}
