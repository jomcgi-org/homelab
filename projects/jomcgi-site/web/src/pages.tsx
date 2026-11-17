import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { GROUPS, hrefFor, sectionsIn } from "./sections";
import { Shell } from "./Shell";

function SectionGrid() {
  return (
    <VStack gap={6}>
      {GROUPS.map((group) => (
        <section key={group} aria-labelledby={`group-${group}`}>
          <VStack gap={3}>
            <Heading level={2} id={`group-${group}`}>
              {group}
            </Heading>
            <ul className="site-cards">
              {sectionsIn(group).map((section) => (
                <li key={section.path}>
                  <Card>
                    <VStack gap={2}>
                      <a className="site-card-link" href={hrefFor(section)}>
                        {section.name}
                      </a>
                      <Text type="body">{section.summary}</Text>
                    </VStack>
                  </Card>
                </li>
              ))}
            </ul>
          </VStack>
        </section>
      ))}
    </VStack>
  );
}

export function HomePage() {
  return (
    <Shell>
      <VStack gap={8}>
        <header className="site-hero">
          <Heading level={1}>Joe McGinley, Scotland.</Heading>
          <Text type="body">
            Building production grade infra for weekend side quests and keeping{" "}
            <a href="https://semgrep.dev">Semgrep</a> online.
          </Text>
        </header>
        <SectionGrid />
      </VStack>
    </Shell>
  );
}

export function NotFoundPage() {
  return (
    <Shell>
      <VStack gap={8}>
        <header className="site-hero">
          <Heading level={1}>This page isn't here.</Heading>
          <Text type="body">
            The site is moving to new addresses. Every section is listed below.
          </Text>
        </header>
        <SectionGrid />
      </VStack>
    </Shell>
  );
}
