import { Heading } from "@astryxdesign/core/Heading";
import { VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { Shell } from "./Shell";
import { UPDATED, policyLinks } from "./policy-data";
import "./scotland.css";

export interface Policy {
  slug: string;
  title: string;
  lead: string;
  description: string;
  sections: {
    id: string;
    label: string;
    heading: string;
    paras: string[];
    rows?: { k: string; v: string; href: string }[];
  }[];
}

const num = (index: number) => String(index + 1).padStart(2, "0");

export function ScotlandPolicyPage({ doc }: { doc: Policy }) {
  return (
    <Shell>
      <VStack gap={8}>
        <nav aria-label="Sections">
          <Text type="body">/ Index</Text>
          <ul className="scotland-links">
            {doc.sections.map((section, index) => (
              <li key={section.id}>
                <a href={`#${section.id}`}>
                  {num(index)} {section.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>
        <section className="scotland-section" aria-labelledby="intro-h">
          <div className="scotland-band">
            <span>Scotland connector</span>
            <span>updated {UPDATED}</span>
          </div>
          <VStack gap={3}>
            <Heading level={1} id="intro-h">
              {doc.title}
            </Heading>
            <Text type="body">{doc.lead}</Text>
            <ul className="scotland-links">
              <li>
                <a href="/scotland">the connector</a>
              </li>
              {policyLinks
                .filter((link) => link.href !== `/scotland/${doc.slug}`)
                .map((link) => (
                  <li key={link.href}>
                    <a href={link.href}>{link.label}</a>
                  </li>
                ))}
            </ul>
          </VStack>
        </section>
        {doc.sections.map((section, index) => (
          <section
            className="scotland-section"
            key={section.id}
            id={section.id}
            aria-labelledby={`${section.id}-h`}
          >
            <div className="scotland-band">
              <span>{num(index)}</span>
              <span>{section.label}</span>
            </div>
            <VStack gap={3}>
              <Heading level={2} id={`${section.id}-h`}>
                {section.heading}
              </Heading>
              {section.paras.map((paragraph) => (
                <Text type="body" key={paragraph}>
                  {paragraph}
                </Text>
              ))}
              {section.rows && (
                <dl className="scotland-spec">
                  {section.rows.map((row) => (
                    <div key={row.k}>
                      <dt>{row.k}</dt>
                      <dd>
                        <a href={row.href}>{row.v}</a>
                      </dd>
                    </div>
                  ))}
                </dl>
              )}
            </VStack>
          </section>
        ))}
      </VStack>
    </Shell>
  );
}
