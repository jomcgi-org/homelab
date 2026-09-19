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

export function label(value) {
  return value
    .split("-")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function monthLabel(value) {
  return MONTH_FORMAT.format(asDate(`${value}-01`));
}

export function monthHref(
  month,
  selectedProject,
  selectedTechnology,
  date = "",
) {
  const params = new URLSearchParams({ month });
  if (selectedProject) params.set("project", selectedProject);
  if (selectedTechnology) params.set("technology", selectedTechnology);
  const fragment = date ? `#update-${date}` : "";
  return `/updates?${params.toString()}${fragment}`;
}

export function facetHref(
  kind,
  value,
  selectedProject,
  selectedTechnology,
  selectedMonth = "",
) {
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
  if (selectedMonth) params.set("month", selectedMonth);
  const query = params.toString();
  return query ? `/updates?${query}` : "/updates";
}
