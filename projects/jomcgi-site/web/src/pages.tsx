import { SlopPage } from "./slop-page";
import { ScotlandSupportPage } from "./scotland-support-page";
import { ScotlandTermsPage } from "./scotland-terms-page";
import { ScotlandPrivacyPage } from "./scotland-privacy-page";
import { ScotlandPage } from "./scotland-page";
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
  "slop.html": SlopPage,
  "scotland/support.html": ScotlandSupportPage,
  "scotland/terms.html": ScotlandTermsPage,
  "scotland/privacy.html": ScotlandPrivacyPage,
  "scotland.html": ScotlandPage,
};
