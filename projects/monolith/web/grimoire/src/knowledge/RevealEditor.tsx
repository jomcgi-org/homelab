import { useMemo, useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import {
  CheckboxList,
  CheckboxListItem,
} from "@astryxdesign/core/CheckboxList";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Selector } from "@astryxdesign/core/Selector";
import { Text } from "@astryxdesign/core/Text";
import { useToast } from "@astryxdesign/core/Toast";
import {
  Typeahead,
  TypeaheadItem,
  type SearchableItem,
  type SearchSource,
} from "@astryxdesign/core/Typeahead";
import { errorMessage } from "../api/client";
import {
  afterGrantChange,
  findEntities,
  previewGrants,
  revealGrants,
  revokeGrant,
  useEntity,
  type EntityProjection,
  type GrantDraft,
  type GrantPreview,
} from "../api/knowledge";
import type { CharacterView } from "../api/schema";
import { Loaded } from "../app/Loaded";
import { useAction } from "../app/useAction";
import { scopeLabel } from "../lib/knowledge-fields.js";
import { fieldLabel } from "./KnowledgeDetails";
import { RevealProjection } from "./RevealProjection";
import { useComposition } from "./useComposition";
import {
  detailsOf,
  emptyDraft,
  isShareable,
  ScopeFields,
  type ScopeDraft,
} from "./ScopeFields";

/** The part of a character a reveal needs: who to address and what to call them. */
export type RevealCharacter = Pick<CharacterView, "id" | "character_name">;

type EntityItem = SearchableItem<{ entityType: string }>;

const EVERYONE = "all";

interface RevealEditorProps {
  campaignId: string;
  characters: RevealCharacter[];
}

/** Find an entity, choose who learns how much, preview it, then share it. */
export function RevealEditor({ campaignId, characters }: RevealEditorProps) {
  const [picked, setPicked] = useState<EntityItem | null>(null);
  const [hideKnownTo, setHideKnownTo] = useState(EVERYONE);
  const toast = useToast();
  const entity = useEntity(campaignId, picked?.id ?? null);
  const composition = useComposition();

  const source = useMemo<SearchSource<EntityItem>>(() => {
    let request: AbortController | undefined;
    const find = async (q: string): Promise<EntityItem[]> => {
      request?.abort();
      const current = new AbortController();
      request = current;
      await composition.settled();
      // A newer keystroke replaced this search while it waited.
      if (current.signal.aborted) return [];
      try {
        const found = await findEntities({
          campaignId,
          q,
          notGrantedTo: hideKnownTo === EVERYONE ? null : hideKnownTo,
          signal: current.signal,
        });
        return found.map((row) => ({
          id: row.id,
          label: row.name,
          auxiliaryData: { entityType: row.entity_type },
        }));
      } catch (error) {
        if (!current.signal.aborted)
          toast({ body: errorMessage(error), type: "error" });
        return [];
      }
    };
    return {
      search: find,
      bootstrap: () => find(""),
      cancel: () => request?.abort(),
    };
  }, [campaignId, hideKnownTo, toast, composition]);

  return (
    <VStack gap={4}>
      <div {...composition.handlers}>
        <Typeahead
          label="Find knowledge"
          placeholder="NPC, place, item…"
          searchSource={source}
          value={picked}
          onChange={setPicked}
          hasEntriesOnFocus
          maxMenuItems={30}
          emptySearchText="No knowledge matches."
          renderItem={(item) => (
            <TypeaheadItem
              item={item}
              description={fieldLabel(item.auxiliaryData?.entityType ?? "")}
            />
          )}
          width="100%"
        />
      </div>
      <Selector
        label="Hide knowledge already granted to"
        options={[
          { value: EVERYONE, label: "Show all" },
          ...characters.map((pc) => ({
            value: pc.id,
            label: pc.character_name,
          })),
        ]}
        value={hideKnownTo}
        onChange={setHideKnownTo}
        width="100%"
      />
      {picked && (
        <Loaded query={entity} what="this knowledge">
          {(loaded) => (
            <EntityReveal
              key={loaded.id}
              campaignId={campaignId}
              entity={loaded}
              characters={characters}
            />
          )}
        </Loaded>
      )}
    </VStack>
  );
}

interface EntityRevealProps {
  campaignId: string;
  /** The DM's projection: every field, plus who already holds a grant. */
  entity: EntityProjection;
  characters: RevealCharacter[];
}

function EntityReveal({ campaignId, entity, characters }: EntityRevealProps) {
  const [selected, setSelected] = useState<string[]>([]);
  const [draft, setDraft] = useState<ScopeDraft>(emptyDraft);
  const [silent, setSilent] = useState(false);
  const [previewed, setPreviewed] = useState<{
    signature: string;
    rows: GrantPreview[];
  } | null>(null);

  const grants = entity.grants ?? [];
  const grantOf = (characterId: string) =>
    grants.find((grant) => grant.player_character_id === characterId);
  const nameOf = (characterId: string) =>
    characters.find((pc) => pc.id === characterId)?.character_name;
  const details = detailsOf(entity, draft);
  const request: GrantDraft = {
    campaignId,
    entityId: entity.id,
    // A character granted since being ticked is dropped: a second grant is refused.
    characterIds: selected.filter((id) => !grantOf(id)),
    scope: draft.scope,
    details,
  };
  // The share button only appears for the exact request that was previewed.
  const signature = JSON.stringify(request);

  const invalidate = afterGrantChange(campaignId);
  const preview = useAction(previewGrants, {
    onSuccess: (rows, input) =>
      setPreviewed({ signature: JSON.stringify(input), rows }),
  });
  const reveal = useAction(revealGrants, {
    invalidate,
    success: "Knowledge shared.",
    onSuccess: () => {
      setSelected([]);
      setPreviewed(null);
    },
  });
  const revoke = useAction(revokeGrant, {
    invalidate,
    success: "Knowledge retracted.",
  });
  const busy = preview.isPending || reveal.isPending || revoke.isPending;

  return (
    <VStack gap={4}>
      <Heading level={3}>{entity.name}</Heading>
      {characters.length === 0 ? (
        <Text type="body" color="secondary">
          This campaign has no characters to share with yet.
        </Text>
      ) : (
        <CheckboxList
          label="Share with"
          value={request.characterIds}
          onChange={setSelected}
        >
          {characters.map((pc) => {
            const grant = grantOf(pc.id);
            return (
              <CheckboxListItem
                key={pc.id}
                value={pc.id}
                label={pc.character_name}
                description={
                  grant && `Already shared: ${scopeLabel(grant.grant_scope)}`
                }
                isDisabled={grant !== undefined}
              />
            );
          })}
        </CheckboxList>
      )}
      <ScopeFields entity={entity} value={draft} onChange={setDraft} />
      <Text type="body" color="secondary">
        Players will see {entity.name}
        {draft.scope === "partial"
          ? ` and: ${draft.clue.trim() || "your selected detail"}`
          : draft.scope === "full"
            ? " and all its knowledge details."
            : " and its type only."}
      </Text>
      <HStack>
        <Button
          label="Preview knowledge"
          isLoading={preview.isPending}
          isDisabled={
            busy ||
            request.characterIds.length === 0 ||
            !isShareable(draft, details)
          }
          onClick={() => preview.run(request)}
        />
      </HStack>
      {previewed?.signature === signature && (
        <VStack gap={3} as="section" aria-label="Recipient previews">
          {previewed.rows.map((row) => (
            <VStack gap={2} key={row.player_character_id}>
              <Heading level={4}>
                {nameOf(row.player_character_id) ?? "Character"} will see
              </Heading>
              <RevealProjection
                knowledge={{
                  projection: row.projection,
                  grant_scope: draft.scope,
                }}
              />
            </VStack>
          ))}
          <HStack>
            <Button
              label="Share knowledge"
              variant="primary"
              isLoading={reveal.isPending}
              isDisabled={busy}
              onClick={() => reveal.run(request)}
            />
          </HStack>
        </VStack>
      )}
      {grants.length > 0 && (
        <VStack gap={2}>
          <CheckboxInput
            label="Retract silently"
            description="The player gets no notice in the story."
            value={silent}
            onChange={setSilent}
          />
          <HStack gap={2} wrap="wrap">
            {grants.map((grant) => (
              <Button
                key={grant.id}
                label={`Retract from ${nameOf(grant.player_character_id) ?? "character"}`}
                variant="destructive"
                size="sm"
                isDisabled={busy}
                onClick={() =>
                  revoke.run({ campaignId, grantId: grant.id, silent })
                }
              />
            ))}
          </HStack>
        </VStack>
      )}
    </VStack>
  );
}
