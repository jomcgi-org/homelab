import index from "../docs/_src/search-index.json";

export function search(query: string) {
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return [];
  return index
    .filter((entry) => terms.every((term) => entry.text.includes(term)))
    .sort(
      (a, b) =>
        Number(b.title.toLowerCase().includes(query.toLowerCase())) -
        Number(a.title.toLowerCase().includes(query.toLowerCase())),
    )
    .slice(0, 12)
    .map(({ slug, title, project, kind }) => ({ slug, title, project, kind }));
}
