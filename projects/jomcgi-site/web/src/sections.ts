// The site's sections and their URLs (HOME-174 URL contract).
// A section is `live` once its pages are served by this site. Until then its
// link points at the current page on jomcgi.dev, so the preview host works.

export const ORIGIN = "https://jomcgi.dev";

export type Group = "Writing" | "Apps" | "Factory" | "About";

export interface Section {
  name: string;
  group: Group;
  /** The section's address in this site: one top-level prefix. */
  path: `/${string}`;
  /** The page that serves this section today, on jomcgi.dev. */
  legacy: `/${string}`;
  live: boolean;
  summary: string;
  /** Draft notes stay addressable without appearing in the site index. */
  listed?: boolean;
}

export const HOME: Section = {
  name: "Home",
  group: "About",
  path: "/",
  legacy: "/",
  live: true,
  summary: "Joe McGinley, Scotland.",
};

export const GROUPS: readonly Group[] = ["Writing", "Apps", "Factory", "About"];

export const SECTIONS: readonly Section[] = [
  {
    name: "Blog",
    group: "Writing",
    path: "/blog",
    legacy: "/blog",
    live: true,
    summary: "Notes on building and running the homelab.",
  },
  {
    name: "Homelab",
    group: "Writing",
    path: "/homelab",
    legacy: "/homelab",
    live: true,
    summary: "What runs, how a threat is contained, and where to read more.",
  },
  {
    name: "Slop",
    group: "Writing",
    path: "/slop",
    legacy: "/slop",
    live: true,
    listed: false,
    summary: "Draft pages. Not listed, wording in flux.",
  },
  {
    name: "Hikes",
    group: "Apps",
    path: "/hikes",
    legacy: "/app/hikes",
    live: true,
    summary: "Scottish walks with their weather windows.",
  },
  {
    name: "Stars",
    group: "Apps",
    path: "/stars",
    legacy: "/app/stars",
    live: true,
    summary: "Dark-sky sites and the nights worth going out.",
  },
  {
    name: "Ships",
    group: "Apps",
    path: "/ships",
    legacy: "/app/ships",
    live: false,
    summary: "Vessels moving around the coast.",
  },
  {
    name: "Trips",
    group: "Apps",
    path: "/trips",
    legacy: "/app/trips",
    // Not live until the hub export commits trips/export.json (HOME-452).
    live: false,
    summary: "Trips, day by day, with photos.",
  },
  {
    name: "LLM leaderboard",
    group: "Apps",
    path: "/llm-leaderboard",
    legacy: "/app/llm-leaderboard",
    live: true,
    // Unlisted until the agent factory supplies its data. The page stays at
    // its address.
    listed: false,
    summary:
      "Models doing real tasks in my monorepos, graded by the repo's own tests.",
  },
  {
    name: "Grimoire",
    group: "Apps",
    path: "/grimoire",
    legacy: "/app/grimoire",
    live: false,
    summary: "A reader for open tabletop rules.",
  },
  {
    name: "Scotland",
    group: "Apps",
    path: "/scotland",
    legacy: "/scotland",
    live: true,
    summary:
      "Plan and check Scottish walks and stargazing with forecast verdicts.",
  },
  {
    name: "Factory",
    group: "Factory",
    path: "/factory",
    legacy: "/slop/factory",
    // Its route, jomcgi.dev/factory*, is in edge/routes.json (HOME-202), so
    // the nav stays on the page this site serves.
    live: true,
    summary: "The software factory: goals, work in flight and what shipped.",
  },
  {
    name: "Engineering",
    group: "About",
    path: "/engineering",
    legacy: "/engineering",
    live: true,
    // Unlisted: Docs covers these systems from the repository. The page
    // stays at its address.
    listed: false,
    summary: "How this site and its cluster are engineered.",
  },
  {
    name: "CV",
    group: "About",
    path: "/cv",
    legacy: "/cv",
    live: true,
    summary: "Work history.",
  },
];

/** Where a link to the section goes right now. */
export function hrefFor(section: Section): string {
  return section.live ? section.path : `${ORIGIN}${section.legacy}`;
}

export function sectionsIn(group: Group): Section[] {
  return SECTIONS.filter(
    (section) => section.group === group && section.listed !== false,
  );
}
