export function pageOf(items, requested) {
  const pages = Math.ceil(items.length / 5);
  const page = Math.max(0, Math.min(requested, pages - 1));
  return { items: items.slice(page * 5, page * 5 + 5), page, pages };
}
