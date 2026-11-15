import { useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useNavigate } from "react-router";
import type { LobbyCampaignView } from "../../api/schema";
import { createOwnCharacter, keys } from "../../api/lobby";
import { paths } from "../../app/paths";
import { useAction } from "../../app/useAction";
import { Invitations } from "./Invitations";
import { Players } from "./Players";

interface CampaignCardProps {
  campaign: LobbyCampaignView;
  canAdministerAccounts: boolean;
  canEnroll: boolean;
}

const roleLabel = (campaign: LobbyCampaignView) =>
  campaign.is_owner ? "Owner · DM" : campaign.role === "dm" ? "DM" : "Player";

/** One campaign: where to go, and for a DM, who is at the table. */
export function CampaignCard({
  campaign,
  canAdministerAccounts,
  canEnroll,
}: CampaignCardProps) {
  const dm = campaign.role === "dm";
  const needsCharacter = !dm && !campaign.player_character_id;
  return (
    <Card>
      <VStack gap={4} as="article" aria-label={campaign.name}>
        <VStack gap={1}>
          <Text type="label" color="secondary">
            {roleLabel(campaign)}
          </Text>
          <Heading level={3}>{campaign.name}</Heading>
          {!dm && campaign.character_name && (
            <Text type="body" color="secondary">
              Your character: {campaign.character_name}
            </Text>
          )}
        </VStack>
        {needsCharacter && <CreateCharacter campaignId={campaign.id} />}
        <HStack gap={2} wrap="wrap">
          <Button
            label="Open session"
            variant="primary"
            href={paths.session(campaign.id)}
          />
          <Button
            label="Character sheets"
            href={paths.sheets(campaign.player_character_id)}
          />
          <Button label="Notes" href={paths.notes(campaign.id)} />
          <Button label="Journal" href={paths.journal(campaign.id)} />
        </HStack>
        {dm && (
          <Collapsible
            defaultIsOpen={false}
            trigger={
              <Text type="label">
                {campaign.is_owner ? "Players and invitations" : "Players"}
              </Text>
            }
          >
            <VStack gap={5} paddingBlockStart={3}>
              <Players campaign={campaign} />
              {campaign.is_owner && (
                <Invitations
                  campaign={campaign}
                  canAdministerAccounts={canAdministerAccounts}
                  canEnroll={canEnroll}
                />
              )}
            </VStack>
          </Collapsible>
        )}
      </VStack>
    </Card>
  );
}

/** A seated player without a character names one, then fills in its sheet. */
function CreateCharacter({ campaignId }: { campaignId: string }) {
  const [name, setName] = useState("");
  const navigate = useNavigate();
  const create = useAction(createOwnCharacter, {
    invalidate: [keys.lobby],
    onSuccess: (character) => navigate(paths.sheets(character.id)),
  });
  const submit = () => {
    if (name.trim()) create.run({ campaignId, name: name.trim() });
  };
  return (
    <HStack gap={2} vAlign="end" wrap="wrap">
      <TextInput
        label="Character name"
        value={name}
        onChange={(value) => setName(value.slice(0, 120))}
        onEnter={submit}
        width={280}
      />
      <Button
        label="Create your character"
        isLoading={create.isPending}
        isDisabled={!name.trim()}
        onClick={submit}
      />
    </HStack>
  );
}
