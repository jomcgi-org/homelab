import { useState } from "react";
import { readBoot, useTrip, photoUrl } from "./trips-data";
import { Elevation, LazyMap, PhotoGrid, TripFrame } from "./trips-ui";
import { dayLabel, groupByDay } from "./trips-model.mjs";
import type { Boot } from "./trips-types";
export function TripDayPage({ initial = readBoot() }: { initial?: Boot } = {}) {
  const state = useTrip(initial),
    trip = initial.trip,
    dayNumber = initial.dayNumber ?? 1;
  const [current, setCurrent] = useState(0);
  const day = initial.days.find((d) => d.dayNumber === dayNumber);
  const photos = state.payload
    ? (groupByDay(state.payload.points, state.payload.trip.tz)
        .find((d) => d.dayNumber === dayNumber)
        ?.points.filter((p) => p.image) ?? [])
    : initial.photos;
  const photo = photos[current];
  async function step(index: number) {
    if (index < 0 || index >= initial.photoCount) return;
    if (index >= photos.length) {
      try {
        await state.load();
      } catch {
        return;
      }
    }
    setCurrent(index);
  }
  return (
    <TripFrame
      boot={initial}
      title={
        trip ? `${trip.short_title ?? trip.title} - Day ${dayNumber}` : "Day"
      }
      {...state}
    >
      {trip && day && (
        <>
          <h2>{dayLabel(trip.days, dayNumber)}</h2>
          <p>
            {day.date} · {day.distance} km · {initial.photoCount} photos
          </p>
          <nav className="trip-controls" aria-label="Days">
            {dayNumber > 1 && (
              <a
                href={`/trips/${encodeURIComponent(trip.slug)}/day/${dayNumber - 1}`}
              >
                ← Previous day
              </a>
            )}
            {dayNumber < initial.days.length && (
              <a
                href={`/trips/${encodeURIComponent(trip.slug)}/day/${dayNumber + 1}`}
              >
                Next day →
              </a>
            )}
          </nav>
          {state.photoBase && photo && (
            <section
              aria-label="Day photo viewer"
              onKeyDown={(event) => {
                if (event.key === "ArrowLeft") void step(current - 1);
                if (event.key === "ArrowRight") void step(current + 1);
              }}
            >
              <img
                className="trip-day-photo"
                src={photoUrl(state.photoBase, photo.image!, "display")}
                alt={`Photo ${current + 1} · ${photo.taken_at}`}
                width="1920"
                height="1080"
              />
              <div className="trip-controls">
                <button
                  type="button"
                  disabled={current === 0}
                  onClick={() => void step(current - 1)}
                >
                  Previous photo
                </button>
                <label>
                  Photo{" "}
                  <input
                    aria-label="Photo position"
                    type="range"
                    min="0"
                    max={Math.max(0, initial.photoCount - 1)}
                    value={current}
                    onChange={(event) => void step(Number(event.target.value))}
                  />
                </label>
                <span>
                  {current + 1} / {initial.photoCount}
                </span>
                <button
                  type="button"
                  disabled={current >= initial.photoCount - 1}
                  onClick={() => void step(current + 1)}
                >
                  Next photo
                </button>
              </div>
              <p>
                {photo.taken_at} · {photo.lat.toFixed(5)},{" "}
                {photo.lng.toFixed(5)}
                {photo.elevation != null
                  ? ` · ${Math.round(photo.elevation)} m`
                  : ""}
                {photo.iso ? ` · ISO ${photo.iso}` : ""}
                {photo.aperture ? ` · f/${photo.aperture}` : ""}
                {photo.shutter_speed ? ` · ${photo.shutter_speed} s` : ""}
              </p>
            </section>
          )}
          <Elevation series={initial.series} />
          <LazyMap
            slug={trip.slug}
            load={async () => {
              const data = await state.load();
              return groupByDay(data.points, data.trip.tz).filter(
                (d) => d.dayNumber === dayNumber,
              );
            }}
          />
          <PhotoGrid photos={photos} base={state.photoBase} />
          {state.photoBase &&
            !state.payload &&
            initial.photoCount > photos.length && (
              <button
                type="button"
                onClick={() => void state.load().catch(() => {})}
              >
                Load all {initial.photoCount} photos
              </button>
            )}
        </>
      )}
      {!day && <p>No points for this day yet.</p>}
    </TripFrame>
  );
}
