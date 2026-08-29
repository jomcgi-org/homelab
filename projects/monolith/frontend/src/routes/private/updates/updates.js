const DATE_FORMAT = new Intl.DateTimeFormat("en-CA", {
  month: "long",
  day: "numeric",
  year: "numeric",
  timeZone: "UTC",
});

const MONTH_FORMAT = new Intl.DateTimeFormat("en-CA", {
  month: "long",
  year: "numeric",
  timeZone: "UTC",
});

function asDate(value) {
  return new Date(`${value}T12:00:00Z`);
}

export function formatDate(value) {
  return DATE_FORMAT.format(asDate(value));
}

export function formatVersion(value) {
  return value.replaceAll("-", ".");
}

export function label(value) {
  return value
    .split("-")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function groupUpdatesByMonth(updates) {
  const groups = [];
  for (const update of updates) {
    const key = update.published_on.slice(0, 7);
    let group = groups.at(-1);
    if (!group || group.key !== key) {
      group = {
        key,
        label: MONTH_FORMAT.format(asDate(`${key}-01`)),
        updates: [],
      };
      groups.push(group);
    }
    group.updates.push(update);
  }
  return groups;
}

export function facetHref(kind, value, selectedProject, selectedTechnology) {
  const params = new URLSearchParams();
  const nextProject =
    kind === "project"
      ? value === selectedProject
        ? null
        : value
      : selectedProject;
  const nextTechnology =
    kind === "technology"
      ? value === selectedTechnology
        ? null
        : value
      : selectedTechnology;
  if (nextProject) params.set("project", nextProject);
  if (nextTechnology) params.set("technology", nextTechnology);
  const query = params.toString();
  return query ? `/updates?${query}` : "/updates";
}
