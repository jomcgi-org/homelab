import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { GROUPS, hrefFor, sectionsIn } from "./sections";

/** Every section as a card, by nav group: the home and 404 pages list them. */
export function SectionGrid() {
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
