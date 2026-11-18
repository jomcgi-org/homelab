import projects from "../docs/_src/projects.json";
import { DocsShell, DocsTabs } from "./docs-shell";

export function DocsIndexPage() {
  return (
    <DocsShell>
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
            <p>{project.project}</p>
            <DocsTabs
              tabs={project.tabs}
              label={`${project.project} documents`}
            />
            <h2>
              <a href={`/docs/${project.slug}`}>{project.title}</a>
            </h2>
            <p>{project.excerpt}</p>
          </article>
        ))}
      </section>
    </DocsShell>
  );
}
