import { SlopPage } from "./slop-page";
import { ScotlandSupportPage } from "./scotland-support-page";
import { ScotlandTermsPage } from "./scotland-terms-page";
import { ScotlandPrivacyPage } from "./scotland-privacy-page";
import { ScotlandPage } from "./scotland-page";
import { BlogPage } from "./blog-page";
import { PostEmberConformancePage } from "./blog-ember-conformance-page";
import { Post125bOnA4090Page } from "./blog-125b-on-a-4090-page";
import { LlmLeaderboardPage } from "./llm-leaderboard-page";
import { DOC_PAGES } from "../docs/_src/pages";
import { DocsIndexPage } from "./docs-index-page";
import { TripsPage } from "./trips-page";
import { TripPage } from "./trip-page";
import { TripDayPage } from "./trip-day-page";
import { TripTimelinePage } from "./trip-timeline-page";
import { EngineeringPage } from "./engineering-page";
import { StarsPage } from "./stars-page";
import { CvPage } from "./cv-page";
import { HomePage } from "./home-page";
import { HikesPage } from "./hikes-page";
import { NotFoundPage } from "./not-found-page";
import { FactoryPage } from "./factory-page";

/**
 * Every page, keyed by its HTML entry. The pre-render and the tests read this
 * registry; entry scripts never import it, so a route ships only its own page.
 * The accessibility test runs axe on every page listed, so a new page cannot
 * ship without the check.
 */
export const PAGES: Record<string, () => React.JSX.Element> = {
  "factory/index.html": FactoryPage,
  "index.html": HomePage,
  "404.html": NotFoundPage,
  "cv.html": CvPage,
  "slop.html": SlopPage,
  "scotland/support.html": ScotlandSupportPage,
  "scotland/terms.html": ScotlandTermsPage,
  "scotland/privacy.html": ScotlandPrivacyPage,
  "scotland.html": ScotlandPage,
  "blog.html": BlogPage,
  "blog/ember-conformance.html": PostEmberConformancePage,
  "blog/125b-on-a-4090.html": Post125bOnA4090Page,
  "llm-leaderboard.html": LlmLeaderboardPage,
  "docs.html": DocsIndexPage,
  ...DOC_PAGES,
  "trips.html": TripsPage,
  "trip.html": TripPage,
  "trip-day.html": TripDayPage,
  "trip-timeline.html": TripTimelinePage,
  "engineering.html": EngineeringPage,
  "hikes.html": HikesPage,
  "stars.html": StarsPage,
};
