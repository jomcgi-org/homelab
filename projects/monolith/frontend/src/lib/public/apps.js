// Single source of truth for the /app/* registry: the APPS nav dropdown
// (Nav.svelte) and the homepage rack (HomepageRack.svelte) both render from
// this list instead of keeping their own copies, so a new app only needs
// adding here once. `featured` marks the two flagship pieces that get a
// full pitch card on the homepage rack, not just a link chip.
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
    href: "/app/firecracker",
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
    desc: "Ask my knowledge graph",
    href: "/app/notes",
  },
];
