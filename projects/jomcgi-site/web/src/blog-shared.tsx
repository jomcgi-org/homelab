export function formatDate(date: string) {
  return new Intl.DateTimeFormat("en-CA", {
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${date}T00:00:00Z`));
}
export function Tags({ tags }: { tags: string[] }) {
  return (
    <ul className="blog-tags" aria-label="Tags">
      {tags.map((tag) => (
        <li key={tag}>
          <a href={`/blog?tag=${tag}`}>{tag}</a>
        </li>
      ))}
    </ul>
  );
}
