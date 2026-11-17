import { Heading } from "@astryxdesign/core/Heading";
import { VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { SectionGrid } from "./SectionGrid";
import { Shell } from "./Shell";

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
