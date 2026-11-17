import { photoUrl, readBoot, useTrip } from "./trips-data";
import { TripFrame } from "./trips-ui";
import type { Boot } from "./trips-types";
export function TripsPage({ initial = readBoot() }: { initial?: Boot } = {}) {
  const state = useTrip(initial);
  const trips = state.index ?? initial.index;
  return (
    <TripFrame boot={initial} title="Trips" {...state}>
      <p>Journeys mapped from GPS-tagged photos.</p>
      <ul className="trip-grid">
        {trips.map((trip) => (
          <li key={trip.slug}>
            <a href={`/trips/${trip.slug}`}>
              {trip.default_image && (
                <img
                  src={photoUrl(state.photoBase, trip.default_image)}
                  alt={trip.title}
                  loading="lazy"
                  width="600"
                  height="400"
                />
              )}
              <h2>{trip.title}</h2>
              {trip.subtitle && <p>{trip.subtitle}</p>}
            </a>
          </li>
        ))}
      </ul>
      {!trips.length && <p>No trips yet.</p>}
    </TripFrame>
  );
}
