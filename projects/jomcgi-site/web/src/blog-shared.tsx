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

/** Tags of projects that no longer run. A post with one stays as history and
 * says so. site-inputs.mjs keeps the same tags off the home page. */
export const RETIRED_PROJECTS: Record<string, string> = {
  embervm: "EmberVM",
};
export function retiredProject(tags: string[]): string | undefined {
  const tag = tags.find((tag) => Object.hasOwn(RETIRED_PROJECTS, tag));
  return tag ? RETIRED_PROJECTS[tag] : undefined;
}
