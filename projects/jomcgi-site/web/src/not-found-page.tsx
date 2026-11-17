import { Heading } from "@astryxdesign/core/Heading";
import { VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { SectionGrid } from "./SectionGrid";
import { Shell } from "./Shell";

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
