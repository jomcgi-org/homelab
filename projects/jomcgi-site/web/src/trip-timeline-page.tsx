import { useEffect, useState } from "react";
import { readBoot, useTrip } from "./trips-data";
import { DayList, LazyMap, PhotoGrid, TripFrame } from "./trips-ui";
import { groupByDay } from "./trips-model.mjs";
import type { Boot } from "./trips-types";
export function TripTimelinePage({
  initial = readBoot(),
}: { initial?: Boot } = {}) {
  const state = useTrip(initial),
    trip = initial.trip;
  const [view, setView] = useState("photos");
  const [selected, setSelected] = useState<string[]>([]);
  const photos = state.payload?.points.filter((p) => p.image) ?? initial.photos;
  const tags = [
    ...new Set(photos.flatMap((p) => p.tags).map((tag) => tag.toLowerCase())),
  ].sort();
  const filtered = selected.length
    ? photos.filter((p) =>
        p.tags.some((tag) => selected.includes(tag.toLowerCase())),
      )
    : photos;
  useEffect(() => {
    if (new URL(window.location.href).searchParams.get("view") === "map")
      setView("map");
  }, []);
  function changeView(value: string) {
    setView(value);
    const url = new URL(window.location.href);
    if (value === "map") url.searchParams.set("view", value);
    else url.searchParams.delete("view");
    window.history.replaceState(null, "", url);
  }
  return (
    <TripFrame
      boot={initial}
      title={trip ? `${trip.short_title ?? trip.title} - Timeline` : "Timeline"}
      {...state}
    >
      {trip && (
        <>
          <DayList boot={initial} />
          <div className="trip-controls">
            <button
              type="button"
              aria-pressed={view === "photos"}
              onClick={() => changeView("photos")}
            >
              Photos
            </button>
            <button
              type="button"
              aria-pressed={view === "map"}
              onClick={() => changeView("map")}
            >
              Map
            </button>
            <p>
              {filtered.length} / {initial.photoCount} photos
            </p>
          </div>
          {view === "map" ? (
            <LazyMap
              slug={trip.slug}
              load={async () => {
                const data = await state.load();
                return groupByDay(data.points, data.trip.tz);
              }}
            />
          ) : (
            <>
              {tags.length > 0 && (
                <fieldset>
                  <legend>Filter photos by tag</legend>
                  {tags.map((tag) => (
                    <label key={tag}>
                      <input
                        type="checkbox"
                        checked={selected.includes(tag)}
                        onChange={() =>
                          setSelected(
                            selected.includes(tag)
                              ? selected.filter((t) => t !== tag)
                              : [...selected, tag],
                          )
                        }
                      />
                      {tag}{" "}
                    </label>
                  ))}
                </fieldset>
              )}
              <PhotoGrid
                key={selected.join(",")}
                photos={filtered}
                base={state.photoBase}
              />
              {!state.payload && initial.photoCount > photos.length && (
                <button
                  type="button"
                  onClick={() => void state.load().catch(() => {})}
                >
                  Load all {initial.photoCount} photos
                </button>
              )}
            </>
          )}
        </>
      )}
    </TripFrame>
  );
}
