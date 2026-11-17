import { Heading } from "@astryxdesign/core/Heading";
import { VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { Shell } from "./Shell";

export function SlopPage() {
  return (
    <Shell>
      <VStack gap={3}>
        <Heading level={1}>Slop</Heading>
        <Text type="body">/ Drafts</Text>
        <Heading level={2}>Pages here are drafts.</Heading>
        <Text type="body">
          They are hosted but not listed, and their wording is still moving.
        </Text>
      </VStack>
    </Shell>
  );
}
