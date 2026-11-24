import projects from "../docs/_src/projects.json";
import { DocsShell, DocsTabs } from "./docs-shell";

/** The generator cuts an excerpt at a fixed length and adds an ellipsis, often
 * mid-sentence. Keep whole sentences when one is there to keep. */
export function wholeSentences(excerpt: string): string {
  if (!excerpt.endsWith("…")) return excerpt;
  const body = excerpt.slice(0, -1);
  const end = Math.max(
    body.lastIndexOf(". "),
    body.lastIndexOf("! "),
    body.lastIndexOf("? "),
  );
  return end >= 40 ? body.slice(0, end + 1) : excerpt;
}

export function DocsIndexPage() {
  return (
    <DocsShell index>
      <header>
        <p>{projects.length} public projects</p>
        <h1>Documentation</h1>
        <p>
          Current-state documentation for the public projects, rendered from the
          repository.
        </p>
      </header>
      <section aria-label="Public projects">
        {projects.map((project) => (
          <article className="docs-project" key={project.project}>
            <h2>
              <a href={`/docs/${project.slug}`}>{project.title}</a>
            </h2>
            <p>{wholeSentences(project.excerpt)}</p>
            <DocsTabs
              tabs={project.tabs}
              label={`${project.project} documents`}
            />
          </article>
        ))}
      </section>
    </DocsShell>
  );
}
