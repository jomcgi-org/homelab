import { useEffect, useRef, useState, type ReactNode } from "react";
import { Shell } from "./Shell";
import { dayLabel } from "./trips-model.mjs";
import { isStale, photoUrl } from "./trips-data";
import type { Boot, Day, Point } from "./trips-types";
import "./trips.css";

export function TripFrame({
  boot,
  title,
  children,
  asOf = boot.asOf,
  now,
  error,
}: {
  boot: Boot;
  title: string;
  children: ReactNode;
  asOf?: string | null;
  now?: number | null;
  error?: string;
}) {
  return (
    <Shell>
      <nav aria-label="Trip">
        <a href="/trips">Trips</a>
        {boot.trip && (
          <>
            {" "}
            /{" "}
            <a href={`/trips/${encodeURIComponent(boot.trip.slug)}`}>
              {boot.trip.short_title ?? boot.trip.title}
            </a>{" "}
            /{" "}
            <a href={`/trips/${encodeURIComponent(boot.trip.slug)}/timeline`}>
              Timeline
            </a>
          </>
        )}
      </nav>
      <h1>{title}</h1>
      {asOf && (
        <p className="trip-freshness">
          Data as of <time dateTime={asOf}>{asOf.replace("T", " ")}</time>
          {now && isStale(asOf, boot.refreshSeconds, now) ? " · Stale" : ""}
        </p>
      )}
      {error && <p role="status">{error}</p>}
      {children}
    </Shell>
  );
}
export function DayList({ boot }: { boot: Boot }) {
  return (
    <section>
      <h2>Daily routes</h2>
      <ol className="trip-days">
        {boot.days.map((day) => (
          <li key={day.dayNumber}>
            <a
              href={`/trips/${encodeURIComponent(boot.trip!.slug)}/day/${day.dayNumber}`}
            >
              {dayLabel(boot.trip!.days, day.dayNumber)}
            </a>
            <span>
              {day.date} · {day.distance} km
              {day.hasElevation ? ` · +${day.ascent} / −${day.descent} m` : ""}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}
export function Elevation({ series }: { series: number[] }) {
  if (series.length < 2) return null;
  const min = Math.min(...series),
    max = Math.max(...series);
  const points = series
    .map(
      (value, i) =>
        `${(i / (series.length - 1)) * 600},${100 - ((value - min) / (max - min || 1)) * 90}`,
    )
    .join(" ");
  return (
    <figure className="trip-elevation">
      <figcaption>
        Elevation · {Math.round(min)}–{Math.round(max)} m
      </figcaption>
      <svg
        viewBox="0 0 600 110"
        role="img"
        aria-label={`Elevation from ${Math.round(min)} to ${Math.round(max)} metres`}
      >
        <polyline points={points} />
      </svg>
    </figure>
  );
}
export function LazyMap({
  load,
  slug,
}: {
  load: () => Promise<Day[]>;
  slug: string;
}) {
  const container = useRef<HTMLDivElement>(null);
  const cleanup = useRef<(() => void) | null>(null);
  const alive = useRef(true);
  const [state, setState] = useState("idle");
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      cleanup.current?.();
    };
  }, []);
  async function open() {
    setState("loading");
    try {
      const [module, days] = await Promise.all([import("./trips-map"), load()]);
      if (!alive.current || !container.current) return;
      cleanup.current = module.showMap(container.current, days, slug, () => {
        cleanup.current?.();
        cleanup.current = null;
        if (alive.current) setState("error");
      });
      setState("ready");
    } catch {
      if (alive.current) setState("error");
    }
  }
  return (
    <section aria-label="Route map">
      {state !== "ready" && (
        <button
          type="button"
          onClick={() => void open()}
          disabled={state === "loading"}
        >
          {state === "loading"
            ? "Loading map…"
            : state === "error"
              ? "Retry map"
              : "Load interactive map"}
        </button>
      )}
      <div
        className={state === "ready" ? "trip-map" : "trip-map-pending"}
        ref={container}
      />
      <p className="trip-credit">
        Map: <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>{" "}
        / <a href="https://openfreemap.org/">OpenFreeMap</a>. Weather from{" "}
        <a href="https://www.met.no/en">MET Norway</a> (
        <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>).
        Climatology from <a href="https://climate.copernicus.eu/">Copernicus</a>
        .
      </p>
    </section>
  );
}
export function PhotoGrid({
  photos,
  base,
}: {
  photos: Point[];
  base: string | null;
}) {
  const [selected, setSelected] = useState<number | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const opener = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (selected !== null) dialog.current?.showModal();
  }, [selected]);
  function close() {
    dialog.current?.close();
    setSelected(null);
    opener.current?.focus();
  }
  if (!base) return null;
  const photo = selected === null ? null : photos[selected];
  return (
    <>
      <ul className="trip-grid">
        {photos.map((p, i) => (
          <li key={p.id}>
            <button
              type="button"
              onClick={(event) => {
                opener.current = event.currentTarget;
                setSelected(i);
              }}
              aria-label={`Open photo ${i + 1}`}
            >
              <img
                src={photoUrl(base, p.image!)}
                alt={`Photo ${i + 1} · ${p.taken_at}`}
                loading="lazy"
                width="600"
                height="400"
              />
            </button>
          </li>
        ))}
      </ul>
      <dialog
        ref={dialog}
        className="trip-lightbox"
        aria-label="Photo viewer"
        onCancel={(event) => {
          event.preventDefault();
          close();
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") {
            event.preventDefault();
            setSelected(Math.max(0, (selected ?? 0) - 1));
          }
          if (event.key === "ArrowRight") {
            event.preventDefault();
            setSelected(Math.min(photos.length - 1, (selected ?? 0) + 1));
          }
        }}
      >
        <button type="button" onClick={close} autoFocus>
          Close photo viewer
        </button>
        {photo && (
          <>
            <img
              src={photoUrl(base, photo.image!, "display")}
              alt={`Photo ${(selected ?? 0) + 1} · ${photo.taken_at}`}
              width="1920"
              height="1080"
            />
            <p>
              {photo.taken_at} · {photo.lat.toFixed(5)}, {photo.lng.toFixed(5)}
              {photo.elevation != null
                ? ` · ${Math.round(photo.elevation)} m`
                : ""}
            </p>
            <a href={photoUrl(base, photo.image!, "full")}>Full size WebP</a>
          </>
        )}
        <div className="trip-controls">
          <button
            type="button"
            disabled={!selected}
            onClick={() => setSelected((selected ?? 0) - 1)}
          >
            Previous photo
          </button>
          <span>
            {selected === null ? 0 : selected + 1} / {photos.length}
          </span>
          <button
            type="button"
            disabled={selected === null || selected >= photos.length - 1}
            onClick={() => setSelected((selected ?? 0) + 1)}
          >
            Next photo
          </button>
        </div>
      </dialog>
    </>
  );
}
