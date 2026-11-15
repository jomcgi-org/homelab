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

export function monthLabel(value) {
  return MONTH_FORMAT.format(asDate(`${value}-01`));
}
