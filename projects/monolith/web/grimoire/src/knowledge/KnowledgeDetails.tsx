import { Markdown } from "@astryxdesign/core/Markdown";
import {
  MetadataList,
  MetadataListItem,
} from "@astryxdesign/core/MetadataList";
import { Text } from "@astryxdesign/core/Text";
import { VStack } from "@astryxdesign/core/Layout";
import type { EntityProjection } from "../api/knowledge";
import { knowledgeFields } from "../lib/knowledge-fields.js";

export const fieldLabel = (key: string) => key.replaceAll("_", " ");

const plain = (value: unknown) =>
  typeof value === "object" && value !== null
    ? JSON.stringify(value)
    : String(value);

function FieldValue({ value }: { value: unknown }) {
  if (typeof value === "string")
    return (
      <Markdown density="compact" headingLevelStart={4}>
        {value}
      </Markdown>
    );
  if (Array.isArray(value))
    return (
      <VStack gap={1} as="ul">
        {value.map((item, index) => (
          <li key={index}>
            <Text type="body">{plain(item)}</Text>
          </li>
        ))}
      </VStack>
    );
  if (typeof value === "object" && value !== null)
    return (
      <MetadataList>
        {Object.entries(value).map(([label, item]) => (
          <MetadataListItem key={label} label={fieldLabel(label)}>
            {plain(item)}
          </MetadataListItem>
        ))}
      </MetadataList>
    );
  return <Text type="body">{String(value)}</Text>;
}

/**
 * The knowledge fields of one projection. It lists exactly what the
 * projection carries: a partial projection shows only its revealed details,
 * and identity and bookkeeping columns are never shown as knowledge.
 */
export function KnowledgeDetails({ entity }: { entity: EntityProjection }) {
  const fields = knowledgeFields(entity) as Array<[string, unknown]>;
  return (
    <VStack gap={3}>
      {entity.recognition_only && (
        <Text type="body">You recognize this name.</Text>
      )}
      {fields.length > 0 && (
        <MetadataList label={{ position: "top" }}>
          {fields.map(([key, value]) => (
            <MetadataListItem key={key} label={fieldLabel(key)}>
              <FieldValue value={value} />
            </MetadataListItem>
          ))}
        </MetadataList>
      )}
    </VStack>
  );
}
