import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { useEffect, useState } from "react";
import { categories, intro, marqueeItems, projects } from "./engineering-data";
import { diagrams } from "./diagrams";
import { Shell } from "./Shell";
import { ORIGIN } from "./sections";
import "./engineering.css";

const numbered = projects.map((project, index) => ({
  ...project,
  num: String(index + 1).padStart(2, "0"),
}));

export function EngineeringPage() {
  const [activeId, setActiveId] = useState("");

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) setActiveId(visible[0].target.id);
      },
      { rootMargin: "-15% 0px -65% 0px" },
    );
    document.querySelectorAll("[data-system]").forEach((record) => {
      observer.observe(record);
    });
    return () => observer.disconnect();
  }, []);

  return (
    <Shell>
      <div className="engineering-page">
        <nav className="engineering-index" aria-label="Systems">
          <p>/ Index</p>
          <ol>
            {numbered.map((project) => (
              <li key={project.id}>
                <a
                  href={`#${project.id}`}
                  aria-current={
                    activeId === project.id ? "location" : undefined
                  }
                >
                  <span className="engineering-number">{project.num}</span>
                  <span>{project.title}</span>
                </a>
              </li>
            ))}
          </ol>
        </nav>

        <div className="engineering-content">
          <header className="engineering-intro">
            <div className="engineering-band">
              <span>{projects.length} systems</span>
              <a href={intro.source.href} target="_blank" rel="noreferrer">
                {intro.source.label}
              </a>
            </div>
            <Heading level={1}>{intro.title}</Heading>
            <Text type="body">{intro.lede}</Text>
            <ul className="engineering-stack" aria-label="Platform">
              {intro.stickers.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
            <ul className="engineering-stack" aria-label="Stack">
              {marqueeItems.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </header>

          <div className="engineering-systems">
            {numbered.map((project) => {
              const Diagram = diagrams[project.id]!;
              return (
                <article
                  key={project.id}
                  id={project.id}
                  data-system={project.id}
                  aria-labelledby={`${project.id}-heading`}
                >
                  <div className="engineering-band">
                    <span className="engineering-number">{project.num}</span>
                    <span>{categories[project.category].label}</span>
                    {project.status && <span>{project.status}</span>}
                    <ul className="engineering-links">
                      {project.links.map((link) => (
                        <li key={link.href}>
                          <a
                            href={new URL(link.href, ORIGIN).href}
                            target={
                              link.href.startsWith("/") ? undefined : "_blank"
                            }
                            rel={
                              link.href.startsWith("/")
                                ? undefined
                                : "noreferrer"
                            }
                          >
                            {link.label}
                            {link.href.startsWith("/") ? "" : " ↗"}
                          </a>
                        </li>
                      ))}
                    </ul>
                  </div>
                  <Heading level={2} id={`${project.id}-heading`}>
                    {project.title}
                  </Heading>
                  <Text type="body">{project.oneLiner}</Text>
                  <Heading level={3}>Motivation</Heading>
                  <Text type="body">{project.motivation}</Text>
                  <Diagram />
                  <dl className="engineering-facts">
                    {project.facts.map((fact) => (
                      <div key={fact.k}>
                        <dt>{fact.k}</dt>
                        <dd>{fact.v}</dd>
                      </div>
                    ))}
                  </dl>
                  {project.snippet && (
                    <pre
                      className="engineering-code"
                      data-lang={project.snippet.lang}
                      tabIndex={0}
                      role="region"
                      aria-label={`${project.title} ${project.snippet.lang} example`}
                    >
                      <code>{project.snippet.code}</code>
                    </pre>
                  )}
                </article>
              );
            })}
          </div>
        </div>
      </div>
    </Shell>
  );
}
