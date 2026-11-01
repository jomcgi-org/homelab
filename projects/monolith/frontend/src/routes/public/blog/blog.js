import { formatDate, monthLabel } from "$lib/public/dates.js";

export { formatDate };

export function groupByMonth(posts) {
  const groups = [];
  for (const post of posts) {
    const key = post.date.slice(0, 7);
    let group = groups.at(-1);
    if (!group || group.key !== key) {
      group = { key, label: monthLabel(key), posts: [] };
      groups.push(group);
    }
    group.posts.push(post);
  }
  return groups;
}
