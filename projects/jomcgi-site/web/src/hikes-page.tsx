import { Heading } from "@astryxdesign/core/Heading";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ComponentType,
} from "react";
import { Shell } from "./Shell";
import { forecastStatus, snapshotStatus } from "../../data/hikes-freshness.mjs";
import { readFilters } from "./hikes/filters";
import "./hikes.css";

// Hydrate the exact inert summary HTML filled by the deploy-time snapshot.
export const PUBLISHED_PLACEHOLDER =
  "<!--hikes-summary:start--><p>Explore Scottish hill walks by duration, distance, ascent and weather window.</p><!--hikes-summary:end-->";

export function HikesPage({
  published = PUBLISHED_PLACEHOLDER,
}: { published?: string } = {}) {
  const summary = useRef<HTMLDivElement>(null);
  const [Planner, setPlanner] = useState<ComponentType | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const explore = useCallback(async () => {
    setLoading(true);
    setError(false);
    try {
      const module = await import("./hikes/planner");
      setPlanner(() => module.HikesPlanner);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    // A shared planner query is intent. Let the page paint before importing it.
    // Ordinary visits (including unrelated query parameters) remain idle.
    let frame = 0;
    const restore = () => {
      cancelAnimationFrame(frame);
      if (
        Object.values(
          readFilters(new URLSearchParams(window.location.search)),
        ).some(Boolean)
      )
        frame = requestAnimationFrame(() => {
          frame = requestAnimationFrame(() => void explore());
        });
    };
    restore();
    window.addEventListener("popstate", restore);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("popstate", restore);
    };
  }, [explore]);
  useEffect(() => {
    const tick = () => {
      const snapshot = summary.current?.querySelector<HTMLElement>(
        "[data-snapshot-freshness]",
      );
      if (snapshot)
        snapshot.textContent = snapshotStatus(
          snapshot.dataset.generatedAt ?? null,
          Date.now(),
        );
      summary.current
        ?.querySelectorAll<HTMLElement>("[data-forecast-freshness]")
        .forEach((status) => {
          status.textContent = forecastStatus(
            status.dataset.fetchedAt ?? null,
            status.dataset.latestFetchedAt ?? null,
            Date.now(),
          );
        });
    };
    tick();
    const timer = setInterval(tick, 60_000);
    return () => clearInterval(timer);
  }, []);
  return (
    <Shell>
      <div className="hikes-page">
        <header>
          <Heading level={1}>
            Hike planner, Scotland walks by weather window
          </Heading>
          <p>
            Scottish hill walks filtered by duration, distance, and ascent, with
            the days the weather allows.
          </p>
        </header>
        {!Planner && (
          <button type="button" onClick={explore} disabled={loading}>
            {loading ? "Loading planner…" : "Explore walks"}
          </button>
        )}
        {error && (
          <p role="status">
            Couldn’t load the planner. Try again; the published walks are below.
          </p>
        )}
        {Planner && <Planner />}
        <details open={!Planner}>
          <summary>Published walks</summary>
          <div
            id="hikes-published"
            ref={summary}
            dangerouslySetInnerHTML={{ __html: published }}
          />
        </details>
        <p className="hikes-credits">
          Walks: <a href="https://www.walkhighlands.co.uk/">WalkHighlands</a>.
          Map:{" "}
          <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> /{" "}
          <a href="https://openfreemap.org/">OpenFreeMap</a>. Weather:{" "}
          <a href="https://www.met.no/">MET Norway</a> (
          <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>).
          Climatology: <a href="https://climate.copernicus.eu/">Copernicus</a>.
        </p>
      </div>
    </Shell>
  );
}
