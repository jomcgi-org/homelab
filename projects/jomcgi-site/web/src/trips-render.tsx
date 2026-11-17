// Bundled for Node at build time; removed from the site copy before deploy.
// Both the build and the R2 refresh use the same React pages as hydration.
import { renderToString } from "react-dom/server";
import { Root } from "./root";
import { TripsPage } from "./trips-page";
import { TripPage } from "./trip-page";
import { TripDayPage } from "./trip-day-page";
import { TripTimelinePage } from "./trip-timeline-page";
import { EMPTY } from "./trips-data";
import {
  elevationSeries,
  groupByDay,
  routeDistanceKm,
} from "./trips-model.mjs";
import type { Boot, Latest, Payload, Trip } from "./trips-types";

export function tripEntries(
  latest: Latest,
  index: { trips: Trip[] },
  payloads: Payload[],
) {
  const common = {
    ...EMPTY,
    renderedAt: latest.generatedAt ? Date.parse(latest.generatedAt) : null,
    asOf: latest.asOf,
    refreshSeconds: latest.refreshSeconds,
    photoBase: latest.photoBase,
  };
  const entries: {
    path: string;
    template: string;
    boot: Boot;
    title: string;
    description?: string;
  }[] = [
    {
      path: "/trips",
      template: "trips",
      boot: { ...common, index: index.trips },
      title: "Trips, GPS photo journeys",
      description:
        "Road trips and journeys mapped from GPS-tagged photos: routes, day-by-day galleries and elevation.",
    },
  ];
  for (const { trip, points } of payloads) {
    const days = groupByDay(points, trip.tz);
    const photos = points.filter((p) => p.image);
    const boot = {
      ...common,
      trip,
      days: days.map(({ points: _points, ...day }) => day),
      photos: photos.slice(0, 12),
      photoCount: photos.length,
      series: elevationSeries(points),
      distance: routeDistanceKm(points),
      maxLat: points.length ? Math.max(...points.map((p) => p.lat)) : undefined,
      coldestTemp: trip.stats?.coldest_temp,
    };
    entries.push({
      path: `/trips/${trip.slug}`,
      template: "trip",
      boot,
      title: trip.title,
      description: trip.subtitle,
    });
    entries.push({
      path: `/trips/${trip.slug}/timeline`,
      template: "trip-timeline",
      boot,
      title: `${trip.short_title ?? trip.title} - Timeline`,
    });
    for (const day of days) {
      const photos = day.points.filter((p) => p.image);
      entries.push({
        path: `/trips/${trip.slug}/day/${day.dayNumber}`,
        template: "trip-day",
        title: `${trip.short_title ?? trip.title} - Day ${day.dayNumber}`,
        boot: {
          ...boot,
          dayNumber: day.dayNumber,
          photos: photos.slice(0, 12),
          photoCount: photos.length,
          series: elevationSeries(day.points),
        },
      });
    }
  }
  return entries;
}
const components = {
  trips: TripsPage,
  trip: TripPage,
  "trip-day": TripDayPage,
  "trip-timeline": TripTimelinePage,
};
export function renderTrip(template: keyof typeof components, boot: Boot) {
  const Page = components[template];
  return renderToString(
    <Root>
      <Page initial={boot} />
    </Root>,
  );
}
