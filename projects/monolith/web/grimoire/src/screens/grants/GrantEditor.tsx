import { useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { Drawer, DrawerHeader } from "@astryxdesign/core/Drawer";
import { Heading } from "@astryxdesign/core/Heading";
import {
  HStack,
  Layout,
  LayoutContent,
  VStack,
} from "@astryxdesign/core/Layout";
import {
  afterGrantChange,
  previewGrants,
  revealGrants,
  revokeGrant,
  updateGrant,
  useEntity,
  type EntityProjection,
  type GrantDraft,
} from "../../api/knowledge";
import type { GrantView } from "../../api/schema";
import { Loaded } from "../../app/Loaded";
import { useAction } from "../../app/useAction";
import type { RevealCharacter } from "../../knowledge/RevealEditor";
import { RevealProjection } from "../../knowledge/RevealProjection";
import {
  detailsOf,
  isShareable,
  ScopeFields,
  type ScopeDraft,
} from "../../knowledge/ScopeFields";

interface GrantEditorProps {
  campaignId: string;
  /** The table row: identity and whether the entity is known to all by default. */
  entity: EntityProjection;
  character: RevealCharacter;
  /** The character's grant on this entity, if one exists. */
  grant: GrantView | undefined;
  onClose: () => void;
}

/** Change, create or retract one character's knowledge of one entity. */
export function GrantEditor({
  campaignId,
  entity,
  character,
  grant,
  onClose,
}: GrantEditorProps) {
  const title = `${entity.name} · ${character.character_name}`;
  const close = (open: boolean) => {
    if (!open) onClose();
  };
  const detail = useEntity(campaignId, entity.id);
  return (
    <Drawer isOpen onOpenChange={close} label={title} purpose="form">
      <Layout
        header={<DrawerHeader title={title} onOpenChange={close} />}
        content={
          <LayoutContent>
            <Loaded query={detail} what="this knowledge">
              {(loaded) => (
                <GrantForm
                  campaignId={campaignId}
                  entity={loaded}
                  character={character}
                  grant={grant}
                  onSaved={onClose}
                />
              )}
            </Loaded>
          </LayoutContent>
        }
      />
    </Drawer>
  );
}

/** The saved grant as a draft; an ungranted global entity starts at its default, full. */
function initialDraft(
  entity: EntityProjection,
  grant: GrantView | undefined,
): ScopeDraft {
  const saved = grant?.revealed_details ?? {};
  return {
    scope: grant?.grant_scope ?? (entity.is_global ? "full" : "name_only"),
    fields: Object.keys(saved).filter((key) => key !== "clue"),
    clue: typeof saved.clue === "string" ? saved.clue : "",
  };
}

interface GrantFormProps {
  campaignId: string;
  /** The DM's full projection, which lists the fields a partial grant can pick. */
  entity: EntityProjection;
  character: RevealCharacter;
  grant: GrantView | undefined;
  onSaved: () => void;
}

function GrantForm({
  campaignId,
  entity,
  character,
  grant,
  onSaved,
}: GrantFormProps) {
  const [draft, setDraft] = useState(() => initialDraft(entity, grant));
  const [silent, setSilent] = useState(false);
  const [previewed, setPreviewed] = useState<{
    signature: string;
    projection: EntityProjection;
  } | null>(null);

  const details = detailsOf(entity, draft);
  const request: GrantDraft = {
    campaignId,
    entityId: entity.id,
    characterIds: [character.id],
    scope: draft.scope,
    details,
  };
  // Confirming is only offered for the exact request that was previewed.
  const signature = JSON.stringify(request);

  const invalidate = afterGrantChange(campaignId);
  const preview = useAction(previewGrants, {
    onSuccess: ([row], input) =>
      row &&
      setPreviewed({
        signature: JSON.stringify(input),
        projection: row.projection,
      }),
  });
  const create = useAction(revealGrants, {
    invalidate,
    success: "Knowledge shared.",
    onSuccess: onSaved,
  });
  const update = useAction(updateGrant, {
    invalidate,
    success: "Knowledge updated.",
    onSuccess: onSaved,
  });
  const revoke = useAction(revokeGrant, {
    invalidate,
    success: "Knowledge retracted.",
    onSuccess: onSaved,
  });
  const busy =
    preview.isPending ||
    create.isPending ||
    update.isPending ||
    revoke.isPending;

  const confirm = () =>
    grant
      ? update.run({
          campaignId,
          grantId: grant.id,
          scope: draft.scope,
          details,
        })
      : create.run(request);

  return (
    <VStack gap={4}>
      <ScopeFields entity={entity} value={draft} onChange={setDraft} />
      <HStack>
        <Button
          label="Preview knowledge"
          isLoading={preview.isPending}
          isDisabled={busy || !isShareable(draft, details)}
          onClick={() => preview.run(request)}
        />
      </HStack>
      {previewed?.signature === signature && (
        <VStack gap={3}>
          <Heading level={3}>What {character.character_name} will see</Heading>
          <RevealProjection
            knowledge={{
              projection: previewed.projection,
              grant_scope: draft.scope,
            }}
          />
          <HStack>
            <Button
              label="Confirm knowledge"
              variant="primary"
              isLoading={create.isPending || update.isPending}
              isDisabled={busy}
              onClick={confirm}
            />
          </HStack>
        </VStack>
      )}
      {grant && (
        <VStack gap={2}>
          <CheckboxInput
            label="Retract silently"
            description="The player gets no notice in the story."
            value={silent}
            onChange={setSilent}
          />
          <HStack>
            <Button
              label="Retract knowledge"
              variant="destructive"
              isDisabled={busy}
              onClick={() =>
                revoke.run({ campaignId, grantId: grant.id, silent })
              }
            />
          </HStack>
        </VStack>
      )}
    </VStack>
  );
}
