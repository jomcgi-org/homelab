import { useState } from "react";
import { useParams } from "react-router";
import { Button } from "@astryxdesign/core/Button";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Item } from "@astryxdesign/core/Item";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { List } from "@astryxdesign/core/List";
import { Selector } from "@astryxdesign/core/Selector";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import {
  useAllEntities,
  useGrants,
  type EntityProjection,
} from "../../api/knowledge";
import { useCharacters, useLobby } from "../../api/lobby";
import type {
  CharacterView,
  GameSessionView,
  GrantView,
} from "../../api/schema";
import { useSessions } from "../../api/session";
import { Loaded } from "../../app/Loaded";
import { Page } from "../../app/Page";
import { paths } from "../../app/paths";
import { fieldLabel } from "../../knowledge/KnowledgeDetails";
import { RevealPanel } from "../../knowledge/RevealPanel";
import { scopeLabel } from "../../lib/knowledge-fields.js";
import { GrantEditor } from "./GrantEditor";

const ANY = "any";

/** The DM's knowledge grants: who knows what, and how much. */
export function Grants() {
  const { campaignId = "" } = useParams();
  const lobby = useLobby();
  const back = { href: paths.session(campaignId), label: "Back to the table" };
  const campaign = lobby.data?.campaigns.find((row) => row.id === campaignId);
  return (
    <Page title="Knowledge at your table" eyebrow={campaign?.name} back={back}>
      <Loaded query={lobby} what="your campaigns">
        {(view) =>
          view.campaigns.some(
            (row) => row.id === campaignId && row.role === "dm",
          ) ? (
            <GrantsTable campaignId={campaignId} />
          ) : (
            <EmptyState
              title="The grants table is for the DM."
              actions={<Button label={back.label} href={back.href} />}
            />
          )
        }
      </Loaded>
    </Page>
  );
}

function GrantsTable({ campaignId }: { campaignId: string }) {
  const characters = useCharacters(campaignId);
  const grants = useGrants(campaignId, true);
  const entities = useAllEntities(campaignId, true);
  const sessions = useSessions(campaignId);
  return (
    <Loaded query={characters} what="the characters">
      {(pcs) => (
        <VStack gap={5}>
          <HStack>
            <RevealPanel campaignId={campaignId} characters={pcs} />
          </HStack>
          <Loaded query={grants} what="the grants">
            {(granted) => (
              <Loaded query={entities} what="the campaign's knowledge">
                {(known) => (
                  <GrantMatrix
                    campaignId={campaignId}
                    characters={pcs}
                    grants={granted}
                    entities={known}
                    sessions={sessions.data ?? []}
                  />
                )}
              </Loaded>
            )}
          </Loaded>
        </VStack>
      )}
    </Loaded>
  );
}

interface GrantMatrixProps {
  campaignId: string;
  characters: CharacterView[];
  grants: GrantView[];
  entities: EntityProjection[];
  sessions: GameSessionView[];
}

/** Every entity against every character. Each pairing opens its grant editor. */
function GrantMatrix({
  campaignId,
  characters,
  grants,
  entities,
  sessions,
}: GrantMatrixProps) {
  const [query, setQuery] = useState("");
  const [type, setType] = useState(ANY);
  const [session, setSession] = useState(ANY);
  const [editing, setEditing] = useState<{
    entityId: string;
    characterId: string;
  } | null>(null);

  const grantFor = (entityId: string, characterId: string) =>
    grants.find(
      (grant) =>
        grant.entity_id === entityId &&
        grant.player_character_id === characterId,
    );
  // A scope is always stated in words; nothing here relies on colour.
  const scopeText = (entity: EntityProjection, characterId: string) => {
    const grant = grantFor(entity.id, characterId);
    if (grant) return scopeLabel(grant.grant_scope);
    return entity.is_global ? "Full by default" : "Not granted";
  };

  const types = [...new Set(entities.map((row) => row.entity_type))].sort();
  const shown = entities.filter(
    (row) =>
      row.name.toLowerCase().includes(query.trim().toLowerCase()) &&
      (type === ANY || row.entity_type === type) &&
      (session === ANY ||
        grants.some(
          (grant) =>
            grant.entity_id === row.id && grant.granted_in_session === session,
        )),
  );
  // Looked up by id so the editor follows a refetch instead of a stale row.
  const editingEntity = entities.find((row) => row.id === editing?.entityId);
  const editingCharacter = characters.find(
    (row) => row.id === editing?.characterId,
  );

  return (
    <VStack gap={4}>
      <HStack gap={3} wrap="wrap" vAlign="end">
        <TextInput
          type="search"
          label="Find knowledge"
          value={query}
          onChange={setQuery}
          hasClear
          width={240}
        />
        <Selector
          label="Entity type"
          options={[
            { value: ANY, label: "All types" },
            ...types.map((value) => ({ value, label: fieldLabel(value) })),
          ]}
          value={type}
          onChange={setType}
          width={200}
        />
        <Selector
          label="Granted in session"
          options={[
            { value: ANY, label: "All sessions" },
            ...sessions.map((row) => ({
              value: row.id,
              label: `${new Date(row.started_at).toLocaleDateString()} · ${row.status}`,
            })),
          ]}
          value={session}
          onChange={setSession}
          width={200}
        />
      </HStack>
      {characters.length === 0 ? (
        <EmptyState
          isCompact
          headingLevel={2}
          title="No characters yet"
          description="Knowledge is granted to characters. Seat a player first."
        />
      ) : shown.length === 0 ? (
        <EmptyState
          isCompact
          headingLevel={2}
          title={
            entities.length
              ? "No knowledge matches these filters."
              : "This campaign has no knowledge yet."
          }
        />
      ) : (
        <VStack gap={2}>
          <Text type="body" color="secondary">
            Choose a character's scope to edit or retract their knowledge.
          </Text>
          <List hasDividers header="Knowledge grants">
            {shown.map((entity) => (
              <Item
                key={entity.id}
                as="li"
                align="start"
                label={entity.name}
                labelLines={2}
                description={
                  <VStack gap={2}>
                    <Text type="supporting">
                      {fieldLabel(entity.entity_type)}
                    </Text>
                    <HStack gap={2} wrap="wrap">
                      {characters.map((pc) => {
                        const scope = scopeText(entity, pc.id);
                        return (
                          <Button
                            key={pc.id}
                            label={`Edit ${entity.name} for ${pc.character_name}: ${scope}`}
                            size="sm"
                            onClick={() =>
                              setEditing({
                                entityId: entity.id,
                                characterId: pc.id,
                              })
                            }
                          >
                            {pc.character_name}: {scope}
                          </Button>
                        );
                      })}
                    </HStack>
                  </VStack>
                }
              />
            ))}
          </List>
        </VStack>
      )}
      {editingEntity && editingCharacter && (
        <GrantEditor
          key={`${editingEntity.id}:${editingCharacter.id}`}
          campaignId={campaignId}
          entity={editingEntity}
          character={editingCharacter}
          grant={grantFor(editingEntity.id, editingCharacter.id)}
          onClose={() => setEditing(null)}
        />
      )}
    </VStack>
  );
}
