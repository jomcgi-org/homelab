import { hrefFor, SECTIONS } from "./sections";

export interface HomePost {
  slug: string;
  title: string;
  date: string;
  summary: string;
}
// Replaced by Vite at build time. Only these four fields from two posts ship.
declare const __HOME_POSTS__: HomePost[];
export const HOME_POSTS = __HOME_POSTS__;

export function sectionHref(name: string): string {
  const section = SECTIONS.find((section) => section.name === name);
  if (!section) throw new Error(`Missing home section: ${name}`);
  return hrefFor(section);
}

// App legends and readout titles follow their section names.
const key = (section: string, small: string) => ({
  section,
  legend: section.toLowerCase(),
  big: section,
  small,
  primary: false,
});

export const HOME_KEYS = [
  {
    section: "Factory",
    legend: "software factory",
    primary: true,
    big: "Slop factory",
    small:
      "Agents doing platform work on this cluster, live. Tag one in Discord and it answers.",
  },
  key("CV", "Ten years, underwriting to platform engineering."),
  key("Docs", "How this is built: cluster, build, agents."),
  key("Grimoire", "A D&D campaign manager built on grants."),
  key("Ships", "Every ship off Scotland right now, from live AIS."),
  key("Stars", "Where the sky will be dark and clear tonight."),
  key("Hikes", "Scottish hill walks, planned by day and weather."),
  key("Trips", "A dash-mounted GoPro turned into a browseable journey."),
];
