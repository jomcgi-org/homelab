import { TripsPage } from "./trips-page";
import { TripPage } from "./trip-page";
import { TripDayPage } from "./trip-day-page";
import { TripTimelinePage } from "./trip-timeline-page";
import { CvPage } from "./cv-page";
import { HomePage } from "./home-page";
import { NotFoundPage } from "./not-found-page";

/**
 * Every page, keyed by its HTML entry. The pre-render and the tests read this
 * registry; entry scripts never import it, so a route ships only its own page.
 * The accessibility test runs axe on every page listed, so a new page cannot
 * ship without the check.
 */
export const PAGES: Record<string, () => React.JSX.Element> = {
  "index.html": HomePage,
  "404.html": NotFoundPage,
  "cv.html": CvPage,
  "trips.html": TripsPage,
  "trip.html": TripPage,
  "trip-day.html": TripDayPage,
  "trip-timeline.html": TripTimelinePage,
};
