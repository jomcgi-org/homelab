import { photoUrl, readBoot, useTrip } from "./trips-data";
import { DayList, Elevation, LazyMap, TripFrame } from "./trips-ui";
import { groupByDay } from "./trips-model.mjs";
import type { Boot } from "./trips-types";
export function TripPage({ initial = readBoot() }: { initial?: Boot } = {}) {
  const state = useTrip(initial),
    trip = initial.trip;
  async function download(type: "json" | "gpx") {
    try {
      const data = await state.load();
      const { downloadRoute } = await import("./trips-download");
      downloadRoute(data, type);
    } catch {
      /* useTrip keeps the saved content and reports the error. */
    }
  }
  return (
    <TripFrame boot={initial} title={trip?.title ?? "Trip"} {...state}>
      {trip?.subtitle && <p>{trip.subtitle}</p>}
      {trip && (
        <>
          <dl className="trip-stats">
            <div>
              <dt>Total distance</dt>
              <dd>{initial.distance} km</dd>
            </div>
            <div>
              <dt>Duration</dt>
              <dd>{initial.days.length} days</dd>
            </div>
            {initial.maxLat != null && (
              <div>
                <dt>Furthest north</dt>
                <dd>{initial.maxLat.toFixed(2)}°N</dd>
              </div>
            )}
            {initial.coldestTemp != null && (
              <div>
                <dt>Coldest temp</dt>
                <dd>{initial.coldestTemp}°C</dd>
              </div>
            )}
          </dl>
          <DayList boot={initial} />
          <Elevation series={initial.series} />
          <LazyMap
            slug={trip.slug}
            load={async () => {
              const data = await state.load();
              return groupByDay(data.points, data.trip.tz);
            }}
          />
          {trip.highlights.length > 0 && (
            <section>
              <h2>Highlights</h2>
              <ul className="trip-grid">
                {trip.highlights.map((h) => (
                  <li key={h.id}>
                    <a href={`/trips/${trip.slug}/day/${h.day}`}>
                      {h.image && (
                        <img
                          src={photoUrl(initial.photoBase, h.image)}
                          alt={h.title}
                          loading="lazy"
                          width="600"
                          height="400"
                        />
                      )}
                      <span>
                        Day {h.day} · {h.title}
                      </span>
                    </a>
                  </li>
                ))}
              </ul>
            </section>
          )}
          <div className="trip-controls">
            <button type="button" onClick={() => void download("gpx")}>
              ↓ GPX
            </button>
            <button type="button" onClick={() => void download("json")}>
              ↓ JSON
            </button>
          </div>
        </>
      )}
      {!initial.days.length && <p>No points for this trip yet.</p>}
    </TripFrame>
  );
}
