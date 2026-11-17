import { Heading } from "@astryxdesign/core/Heading";
import { VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import {
  LOCATION,
  contact,
  earlierCareer,
  jobs,
  name,
  personalIntro,
  projects,
  skills,
  summary,
  tagline,
} from "./cv-data";
import { Inline } from "./Inline";
import { Shell } from "./Shell";

const IDENTITY = [
  "Senior Platform Engineer @ Semgrep",
  "AWS / EKS",
  "eBPF & Cilium",
  "Distributed Systems",
  "OpenTelemetry Contributor",
  "K3S Homelab",
  `${LOCATION.short}, ${LOCATION.country}`,
];

const PARTS = [
  { id: "profile", label: "Profile" },
  { id: "experience", label: "Work Experience" },
  { id: "personal", label: "Personal Engineering" },
  { id: "expertise", label: "Technical Expertise" },
];

function Tags({ items, label }: { items: string[]; label?: string }) {
  return (
    <ul className="cv-tags" aria-label={label}>
      {items.map((item) => (
        <li key={item}>{item}</li>
      ))}
    </ul>
  );
}

function Bullets({ items }: { items: string[] }) {
  return (
    <ul className="cv-bullets">
      {items.map((item) => (
        <li key={item}>
          <Inline text={item} />
        </li>
      ))}
    </ul>
  );
}

export function CvPage() {
  return (
    <Shell>
      <VStack gap={8}>
        <header className="site-hero cv-head">
          <Text type="body">Curriculum Vitae · {contact.location}</Text>
          <Heading level={1}>{name}</Heading>
          <Text type="body">{tagline}</Text>
          <ul className="cv-contact">
            <li>
              <a href={`mailto:${contact.email}`}>{contact.email}</a>
            </li>
            <li>
              <a href={contact.linkedin.href} target="_blank" rel="noreferrer">
                {contact.linkedin.label}
              </a>
            </li>
            <li>
              <a href={contact.github.href} target="_blank" rel="noreferrer">
                {contact.github.label}
              </a>
            </li>
          </ul>
        </header>

        <nav aria-label="On this page">
          <ul className="cv-index">
            {PARTS.map((part) => (
              <li key={part.id}>
                <a href={`#${part.id}`}>{part.label}</a>
              </li>
            ))}
          </ul>
        </nav>

        <section id="profile" aria-labelledby="profile-heading">
          <VStack gap={3}>
            <Heading level={2} id="profile-heading">
              Profile
            </Heading>
            <Text type="body">
              <Inline text={summary} />
            </Text>
            <Tags items={IDENTITY} label="Background" />
          </VStack>
        </section>

        <section id="experience" aria-labelledby="experience-heading">
          <VStack gap={6}>
            <Heading level={2} id="experience-heading">
              Work Experience
            </Heading>
            {jobs.map((job) => (
              <article key={job.company} className="cv-job">
                <VStack gap={3}>
                  <div className="cv-job-head">
                    <Heading level={3}>{job.company}</Heading>
                    <span className="cv-dates">{job.dates}</span>
                  </div>
                  <p className="cv-role">{job.title}</p>
                  {job.blurb && (
                    <Text type="body">
                      <Inline text={job.blurb} />
                    </Text>
                  )}
                  {job.bullets && <Bullets items={job.bullets} />}
                  {job.highlights?.map((highlight) => (
                    <VStack key={highlight.title} gap={3}>
                      <Heading level={4}>{highlight.title}</Heading>
                      {highlight.intro && (
                        <Text type="body">
                          <Inline text={highlight.intro} />
                        </Text>
                      )}
                      <Bullets items={highlight.bullets} />
                    </VStack>
                  ))}
                </VStack>
              </article>
            ))}
            <article className="cv-job">
              <VStack gap={3}>
                <Heading level={3}>Earlier Career</Heading>
                <Text type="body">
                  <Inline text={earlierCareer} />
                </Text>
              </VStack>
            </article>
          </VStack>
        </section>

        <section id="personal" aria-labelledby="personal-heading">
          <VStack gap={3}>
            <Heading level={2} id="personal-heading">
              Personal Engineering
            </Heading>
            <Text type="body">
              <Inline text={personalIntro} />
            </Text>
            <Bullets items={projects} />
          </VStack>
        </section>

        <section id="expertise" aria-labelledby="expertise-heading">
          <VStack gap={3}>
            <Heading level={2} id="expertise-heading">
              Technical Expertise
            </Heading>
            <dl className="cv-skills">
              {skills.map((category) => (
                <div key={category.label}>
                  <dt>{category.label}</dt>
                  <dd>
                    <Tags items={category.items} />
                  </dd>
                </div>
              ))}
            </dl>
          </VStack>
        </section>
      </VStack>
    </Shell>
  );
}
