// Single source of truth for the /app/* registry: the site index spine and
// the homepage keypad look apps up here instead of keeping their own copies,
// so a new app only needs adding here once (the keypad also wants a line for
// it in +page.svelte).
// `featured` marks the two flagship pieces; the homepage no longer renders
// pitch cards from it.
export const apps = [
  {
    slug: "grimoire",
    label: "Grimoire",
    desc: "D&D campaign manager",
    href: "/app/grimoire",
    featured: true,
  },
  {
    slug: "firecracker",
    label: "Firecracker",
    desc: "microVM snapshot restore, explained",
    href: "/ember/firecracker",
    featured: true,
  },
  {
    slug: "trips",
    label: "Trips",
    desc: "Geotagged photo journeys",
    href: "/app/trips",
  },
  {
    slug: "hikes",
    label: "Hikes",
    desc: "Scottish hill-walk planner",
    href: "/app/hikes",
  },
  {
    slug: "ships",
    label: "Ships",
    desc: "Live AIS vessel tracker",
    href: "/app/ships",
  },
  {
    slug: "stars",
    label: "Stars",
    desc: "Scotland dark-sky planner",
    href: "/app/stars",
  },
  {
    slug: "notes",
    label: "Notes",
    desc: "Browse my public notes",
    href: "/app/notes",
  },
];
