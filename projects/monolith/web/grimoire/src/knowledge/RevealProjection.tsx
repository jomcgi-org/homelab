import { VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import type { EntityProjection } from "../api/knowledge";
import { projectionOf, scopeOf } from "../lib/knowledge-fields.js";
import { fieldLabel, KnowledgeDetails } from "./KnowledgeDetails";

/** One revealed entity as a reveal event carries it. */
export interface RevealedKnowledge {
  entity_id: string;
  name: string;
  grant_scope?: "name_only" | "partial" | "full" | string;
  retracted?: boolean;
  silent?: boolean;
  [key: string]: unknown;
}

/** A projection the DM previews before it is granted. */
export interface PreviewedKnowledge {
  projection: EntityProjection;
  grant_scope: "name_only" | "partial" | "full";
}

export interface RevealProjectionProps {
  knowledge: RevealedKnowledge | PreviewedKnowledge;
}

/**
 * What a player was shown: name, type and any revealed details. The DM's
 * preview and the player's feed entry both render through it, so the preview
 * cannot promise more or less than the player gets.
 */
export function RevealProjection({ knowledge }: RevealProjectionProps) {
  const projection = projectionOf(knowledge) as EntityProjection;
  const scope: string = scopeOf(knowledge);
  return (
    <VStack gap={2}>
      <Text type="body">
        <strong>{projection.name}</strong>
        {projection.entity_type && ` · ${fieldLabel(projection.entity_type)}`}
      </Text>
      {scope === "name_only" ? (
        <Text type="body">You recognize this name.</Text>
      ) : (
        <KnowledgeDetails entity={projection} />
      )}
    </VStack>
  );
}
