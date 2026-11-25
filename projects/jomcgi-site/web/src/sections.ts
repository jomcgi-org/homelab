// The site's sections and their URLs (HOME-174 URL contract).
// A section is `live` once its pages are served by this site. Until then no
// page links it: the hub pages that served the others are retired, and
// jomcgi.dev/* answers their old URLs with the 404 page (HOME-261).

export const ORIGIN = "https://jomcgi.dev";

export type Group = "Writing" | "Apps" | "Factory" | "About";

export interface Section {
  name: string;
  group: Group;
  /** The section's address in this site: one top-level prefix. */
  path: `/${string}`;
  live: boolean;
  summary: string;
  /** Draft notes stay addressable without appearing in the site index. */
  listed?: boolean;
  /** HTML entries the build emits only while the section is live. */
  entries?: readonly string[];
}

export const HOME: Section = {
  name: "Home",
  group: "About",
  path: "/",
  live: true,
  summary: "Joe McGinley, Scotland.",
};

export const GROUPS: readonly Group[] = ["Writing", "Apps", "Factory", "About"];

export const SECTIONS: readonly Section[] = [
  {
    name: "Blog",
    group: "Writing",
    path: "/blog",
    live: true,
    summary: "Notes on building and running the homelab.",
  },
  {
    name: "Homelab",
    group: "Writing",
    path: "/homelab",
    live: true,
    summary: "What runs, how a threat is contained, and where to read more.",
  },
  {
    name: "Slop",
    group: "Writing",
    path: "/slop",
    live: true,
    listed: false,
    summary: "Draft pages. Not listed, wording in flux.",
  },
  {
    name: "Hikes",
    group: "Apps",
    path: "/hikes",
    live: true,
    summary: "Scottish walks with their weather windows.",
  },
  {
    name: "Stars",
    group: "Apps",
    path: "/stars",
    live: true,
    summary: "Dark-sky sites and the nights worth going out.",
  },
  {
    name: "Ships",
    group: "Apps",
    path: "/ships",
    // Not live until its data reaches the page (HOME-411). The browser read
    // of the public mirror has no CORS header, so the build ships no page.
    live: false,
    entries: ["app/ships.html"],
    summary: "Vessels moving around the coast.",
  },
  {
    name: "Trips",
    group: "Apps",
    path: "/trips",
    // Not live until the hub export commits trips/export.json (HOME-452).
    live: false,
    summary: "Trips, day by day, with photos.",
  },
  {
    name: "LLM leaderboard",
    group: "Apps",
    path: "/llm-leaderboard",
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
    live: false,
    summary: "A reader for open tabletop rules.",
  },
  {
    name: "Scotland",
    group: "Apps",
    path: "/scotland",
    live: true,
    summary:
      "Plan and check Scottish walks and stargazing with forecast verdicts.",
  },
  {
    name: "Factory",
    group: "Factory",
    path: "/factory",
    live: true,
    summary: "The software factory: goals, work in flight and what shipped.",
  },
  {
    name: "Engineering",
    group: "About",
    path: "/engineering",
    live: true,
    // Unlisted: the Homelab page covers these systems and links to the
    // repository for detail. The page stays at its address.
    listed: false,
    summary: "How this site and its cluster are engineered.",
  },
  {
    name: "CV",
    group: "About",
    path: "/cv",
    live: true,
    summary: "Work history.",
  },
];

/** False for an entry of a section that is not live: the build skips it. */
export function built(entry: string): boolean {
  return !SECTIONS.some(
    (section) => !section.live && section.entries?.includes(entry),
  );
}

/** Where a link to the section goes. Only a live section has one. */
export function hrefFor(section: Section): string {
  if (!section.live) throw new Error(`${section.name} is not live`);
  return section.path;
}

/** The live, listed sections of a nav group. */
export function sectionsIn(group: Group): Section[] {
  return SECTIONS.filter(
    (section) =>
      section.group === group && section.live && section.listed !== false,
  );
}
