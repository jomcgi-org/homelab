import { useEffect, useState } from "react";
import { Shell } from "./Shell";
import {
  REPO,
  REVIEWED,
  STALE_AFTER_DAYS,
  STATUS_LABEL,
  containmentIntro,
  groups,
  intro,
  layout,
  limits,
  method,
  paths,
  positions,
  principles,
  repoDir,
  reviewAge,
} from "./homelab-data";
import "./homelab.css";

const reviewedOn = new Date(`${REVIEWED}T12:00:00Z`).toLocaleDateString(
  "en-GB",
  { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" },
);

const PARTS = [
  { id: "fit", label: "How it fits" },
  { id: "containment", label: "Containing a threat" },
  { id: "method", label: "Method" },
  { id: "projects", label: "Projects" },
];

/** How many days old the review is, known only in the browser. The page is
 * built once and viewed later, so the age is read at view time; the built
 * HTML never claims a freshness it cannot know. */
function useReviewAge() {
  const [age, setAge] = useState<number | null>(null);
  useEffect(() => setAge(reviewAge(REVIEWED, Date.now())), []);
  return age;
}

export function HomelabPage() {
  const age = useReviewAge();
  return (
    <Shell current="/homelab">
      <article className="homelab">
        <header>
          <h1>The homelab</h1>
          <p className="homelab-lede">{intro}</p>
          <p className="homelab-meta">
            Reviewed <time dateTime={REVIEWED}>{reviewedOn}</time> ·{" "}
            <a href={REPO}>Source in the repository</a>
          </p>
          {age !== null && age > STALE_AFTER_DAYS && (
            <p className="homelab-stale" role="note">
              This page was last reviewed {age} days ago. Treat what it says as
              history until it is checked again.
            </p>
          )}
          <nav aria-label="On this page">
            <ul className="homelab-index">
              {PARTS.map((part) => (
                <li key={part.id}>
                  <a href={`#${part.id}`}>{part.label}</a>
                </li>
              ))}
            </ul>
          </nav>
        </header>

        <section id="fit" aria-labelledby="fit-heading">
          <h2 id="fit-heading">How it fits</h2>
          <dl className="homelab-layout">
            {layout.map((part) => (
              <div key={part.title}>
                <dt>{part.title}</dt>
                <dd>{part.body}</dd>
              </div>
            ))}
          </dl>
          <h3>Three paths that matter for safety</h3>
          <div className="homelab-paths">
            {paths.map((path) => (
              <section key={path.id} aria-labelledby={`path-${path.id}`}>
                <h4 id={`path-${path.id}`}>{path.title}</h4>
                <p>{path.summary}</p>
                <ol className="homelab-steps">
                  {path.steps.map((step, index) => (
                    <li key={step}>
                      <span className="homelab-step">{step}</span>
                      {index < path.steps.length - 1 && (
                        <span className="homelab-arrow" aria-hidden="true" />
                      )}
                    </li>
                  ))}
                </ol>
              </section>
            ))}
          </div>
        </section>

        <section id="containment" aria-labelledby="containment-heading">
          <h2 id="containment-heading">Containing a threat</h2>
          <p>{containmentIntro}</p>
          <ol className="homelab-principles">
            {principles.map((principle) => (
              <li key={principle.id}>
                <h3>{principle.title}</h3>
                <p>{principle.body}</p>
              </li>
            ))}
          </ol>
          <h3>How far each position can reach</h3>
          <p>By design. These are claims about the design, not test results.</p>
          <dl className="homelab-positions">
            {positions.map((position) => (
              <div key={position.who}>
                <dt>{position.who}</dt>
                <dd>{position.reach}</dd>
              </div>
            ))}
          </dl>
          <h3>What this does not cover</h3>
          <ul className="homelab-limits">
            {limits.map((limit) => (
              <li key={limit}>{limit}</li>
            ))}
          </ul>
        </section>

        <section id="method" aria-labelledby="method-heading">
          <h2 id="method-heading">Method</h2>
          <p>{method.body}</p>
          <p>
            {method.links.map((link, index) => (
              <span key={link.href}>
                {index > 0 && " · "}
                <a href={link.href}>{link.label}</a>
              </span>
            ))}
          </p>
        </section>

        <section id="projects" aria-labelledby="projects-heading">
          <h2 id="projects-heading">Projects</h2>
          <p>
            Each name links to the project's directory, where its README or
            architecture notes live.
          </p>
          {groups.map((group) => (
            <section
              key={group.title}
              aria-labelledby={`group-${group.title.replace(/\W+/g, "-")}`}
            >
              <h3 id={`group-${group.title.replace(/\W+/g, "-")}`}>
                {group.title}
              </h3>
              <ul className="homelab-projects">
                {group.projects.map((project) => (
                  <li key={project.name}>
                    <div className="homelab-project-head">
                      <a href={repoDir(project.path, project.ref)}>{project.name}</a>
                      <span
                        className="homelab-status"
                        data-status={project.status}
                      >
                        {STATUS_LABEL[project.status]}
                      </span>
                    </div>
                    <p>{project.what}</p>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </section>
      </article>
    </Shell>
  );
}
