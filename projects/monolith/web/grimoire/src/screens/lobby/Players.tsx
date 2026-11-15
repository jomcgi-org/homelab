import { useState } from "react";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Button } from "@astryxdesign/core/Button";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Heading } from "@astryxdesign/core/Heading";
import { Item } from "@astryxdesign/core/Item";
import {
  HStack,
  Layout,
  LayoutContent,
  LayoutFooter,
  StackItem,
  VStack,
} from "@astryxdesign/core/Layout";
import { List } from "@astryxdesign/core/List";
import { Selector } from "@astryxdesign/core/Selector";
import { TextInput } from "@astryxdesign/core/TextInput";
import type {
  CharacterView,
  LobbyCampaignView,
  MemberView,
} from "../../api/schema";
import {
  assignCharacter,
  keys,
  removeMember,
  useCharacters,
  useMembers,
  type Assignment,
} from "../../api/lobby";
import { Loaded } from "../../app/Loaded";
import { useAction } from "../../app/useAction";

/** The DM's roster: who is seated, which character each plays. */
export function Players({ campaign }: { campaign: LobbyCampaignView }) {
  const members = useMembers(campaign.id);
  const characters = useCharacters(campaign.id);
  const [managing, setManaging] = useState<MemberView | null>(null);
  const [removing, setRemoving] = useState<MemberView | null>(null);
  const remove = useAction(removeMember, {
    invalidate: [keys.members(campaign.id)],
    success: "Player removed.",
    onSuccess: () => setRemoving(null),
  });
  return (
    <VStack gap={2}>
      <Heading level={4}>Players</Heading>
      <Loaded query={members} what="the players">
        {(rows) => (
          <List hasDividers>
            {rows.map((member) => (
              <Item
                key={member.id}
                as="li"
                label={member.email}
                description={`${member.role === "dm" ? "DM" : "Player"} · ${
                  member.character_name || "No character"
                }`}
                labelLines={2}
                endContent={
                  member.role === "player" && (
                    <Button
                      label="Manage"
                      size="sm"
                      onClick={() => setManaging(member)}
                    />
                  )
                }
              />
            ))}
          </List>
        )}
      </Loaded>
      {managing && (
        <ManagePlayer
          campaignId={campaign.id}
          member={managing}
          members={members.data ?? []}
          characters={characters.data ?? []}
          onRemove={
            campaign.is_owner
              ? () => {
                  setRemoving(managing);
                  setManaging(null);
                }
              : undefined
          }
          onClose={() => setManaging(null)}
        />
      )}
      <AlertDialog
        isOpen={removing !== null}
        onOpenChange={(open) => !open && setRemoving(null)}
        title="Remove this player?"
        description={`${removing?.email ?? ""} loses access to ${campaign.name} on their next request. Their character and its knowledge stay.`}
        actionLabel="Remove player"
        isActionLoading={remove.isPending}
        onAction={() =>
          removing &&
          remove.run({ campaignId: campaign.id, memberId: removing.id })
        }
      />
    </VStack>
  );
}

interface ManagePlayerProps {
  campaignId: string;
  member: MemberView;
  members: MemberView[];
  characters: CharacterView[];
  /** Present for the owner, who may remove a player. */
  onRemove?: () => void;
  onClose: () => void;
}

/** Seat an unassigned character, create a new one, clear the seat, or remove the player. */
function ManagePlayer({
  campaignId,
  member,
  members,
  characters,
  onRemove,
  onClose,
}: ManagePlayerProps) {
  const [existing, setExisting] = useState("");
  const [name, setName] = useState("");
  const assign = useAction(assignCharacter, {
    invalidate: [
      keys.members(campaignId),
      keys.characters(campaignId),
      keys.lobby,
    ],
    success: "Character assignment saved.",
    onSuccess: onClose,
  });
  const seated = new Set(members.map((row) => row.player_character_id));
  const unassigned = characters.filter((row) => !seated.has(row.id));
  const save = (assignment: Assignment) =>
    assign.run({ campaignId, memberId: member.id, assignment });
  return (
    <Dialog
      isOpen
      onOpenChange={(open) => !open && onClose()}
      purpose="form"
      width={480}
    >
      <Layout
        header={
          <DialogHeader
            title={member.email}
            subtitle={member.character_name ?? "No character assigned"}
            onOpenChange={onClose}
          />
        }
        content={
          <LayoutContent>
            <VStack gap={5}>
              <HStack gap={2} vAlign="end">
                <Selector
                  label="Unassigned character"
                  placeholder={
                    unassigned.length ? "Choose a character" : "None available"
                  }
                  isDisabled={unassigned.length === 0}
                  options={unassigned.map((row) => ({
                    value: row.id,
                    label: row.character_name,
                  }))}
                  value={existing}
                  onChange={setExisting}
                  width="100%"
                />
                <StackItem size="static">
                  <Button
                    label="Assign"
                    isDisabled={!existing || assign.isPending}
                    onClick={() => save({ player_character_id: existing })}
                  />
                </StackItem>
              </HStack>
              <HStack gap={2} vAlign="end">
                <TextInput
                  label="New character name"
                  value={name}
                  onChange={(value) => setName(value.slice(0, 120))}
                  width="100%"
                />
                <StackItem size="static">
                  <Button
                    label="Create and assign"
                    isDisabled={!name.trim() || assign.isPending}
                    onClick={() => save({ new: { name: name.trim() } })}
                  />
                </StackItem>
              </HStack>
            </VStack>
          </LayoutContent>
        }
        footer={
          <LayoutFooter>
            <HStack gap={2} hAlign="end" wrap="wrap">
              {onRemove && (
                <Button
                  label="Remove player"
                  variant="destructive"
                  onClick={onRemove}
                />
              )}
              {member.player_character_id && (
                <Button
                  label="Clear assignment"
                  variant="ghost"
                  isDisabled={assign.isPending}
                  onClick={() => save({ player_character_id: null })}
                />
              )}
              <Button label="Close" onClick={onClose} />
            </HStack>
          </LayoutFooter>
        }
      />
    </Dialog>
  );
}
