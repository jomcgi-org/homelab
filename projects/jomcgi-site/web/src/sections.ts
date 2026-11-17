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
}

export const GROUPS: readonly Group[] = ["Writing", "Apps", "Factory", "About"];

export const SECTIONS: readonly Section[] = [
  {
    name: "Blog",
    group: "Writing",
    path: "/blog",
    legacy: "/blog",
    live: false,
    summary: "Notes on building and running the homelab.",
  },
  {
    name: "Docs",
    group: "Writing",
    path: "/docs",
    legacy: "/docs",
    live: false,
    summary: "How the pieces fit together.",
  },
  {
    name: "Hikes",
    group: "Apps",
    path: "/hikes",
    legacy: "/app/hikes",
    live: false,
    summary: "Scottish walks with their weather windows.",
  },
  {
    name: "Stars",
    group: "Apps",
    path: "/stars",
    legacy: "/app/stars",
    live: false,
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
    live: false,
    summary: "Trips, day by day, with photos.",
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
    name: "Factory",
    group: "Factory",
    path: "/factory",
    legacy: "/slop/factory",
    live: false,
    summary: "The software factory: goals, work in flight and what shipped.",
  },
  {
    name: "Engineering",
    group: "About",
    path: "/engineering",
    legacy: "/engineering",
    live: false,
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
  return SECTIONS.filter((section) => section.group === group);
}
