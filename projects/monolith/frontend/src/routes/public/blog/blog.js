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

export function groupByMonth(posts) {
  const groups = [];
  for (const post of posts) {
    const key = post.date.slice(0, 7);
    let group = groups.at(-1);
    if (!group || group.key !== key) {
      group = {
        key,
        label: MONTH_FORMAT.format(asDate(`${key}-01`)),
        posts: [],
      };
      groups.push(group);
    }
    group.posts.push(post);
  }
  return groups;
}
